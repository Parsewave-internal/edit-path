/*
    SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
    SPDX-License-Identifier: GPL-3.0-only
*/

#include "videopathrecorder.hpp"

#include "config-kdenlive.h"
#include "core.h"
#include "assets/model/assetparametermodel.hpp"
#include "effects/effectstack/model/effectstackmodel.hpp"
#include "mainwindow.h"
#include "timeline2/model/timelinemodel.hpp"
#include "timeline2/view/timelinewidget.h"

#include <QAction>
#include <QApplication>
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QDomDocument>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QKeyEvent>
#include <QKeySequence>
#include <QMenu>
#include <QMouseEvent>
#include <QMutexLocker>
#include <QProcess>
#include <QSaveFile>
#include <QShortcutEvent>
#include <QSysInfo>
#include <QTimer>
#include <QToolButton>
#include <QUuid>
#include <QWidget>
#include <QtConcurrent/QtConcurrentRun>

#ifdef VIDEOPATH_HAVE_ZSTD
#include <zstd.h>
#endif

namespace {
QJsonObject canonicalXmlElement(const QDomElement &element)
{
    QJsonObject result;
    result.insert(QStringLiteral("name"), element.tagName());

    QMap<QString, QString> sortedAttributes;
    const QDomNamedNodeMap attributes = element.attributes();
    for (int index = 0; index < attributes.count(); ++index) {
        const QDomAttr attribute = attributes.item(index).toAttr();
        sortedAttributes.insert(attribute.name(), attribute.value());
    }
    QJsonObject serializedAttributes;
    for (auto iterator = sortedAttributes.cbegin(); iterator != sortedAttributes.cend(); ++iterator) {
        serializedAttributes.insert(iterator.key(), iterator.value());
    }
    result.insert(QStringLiteral("attributes"), serializedAttributes);

    QJsonArray children;
    QString textContent;
    for (QDomNode child = element.firstChild(); !child.isNull(); child = child.nextSibling()) {
        if (child.isElement()) {
            children.append(canonicalXmlElement(child.toElement()));
        } else if (child.isText() || child.isCDATASection()) {
            textContent.append(child.nodeValue());
        }
    }
    if (!children.isEmpty()) {
        result.insert(QStringLiteral("children"), children);
    }
    if (!textContent.isEmpty()) {
        result.insert(QStringLiteral("text"), textContent);
    }
    return result;
}

QJsonArray canonicalEffectStack(const std::shared_ptr<EffectStackModel> &stack)
{
    QJsonArray effects;
    if (!stack) {
        return effects;
    }
    QDomDocument document;
    const QDomElement root = stack->toXml(document);
    for (QDomElement effect = root.firstChildElement(); !effect.isNull(); effect = effect.nextSiblingElement()) {
        effects.append(canonicalXmlElement(effect));
    }
    return effects;
}

QString checkpointVideoEncoder()
{
    static const QString selected = []() {
        const QString configured = qEnvironmentVariable("KDENLIVE_VIDEO_PATH_VIDEO_ENCODER");
        if (!configured.isEmpty()) {
            return configured;
        }
        const QString ffmpeg = qEnvironmentVariable("KDENLIVE_VIDEO_PATH_FFMPEG", QStringLiteral("ffmpeg"));
        QProcess process;
        process.start(ffmpeg, {QStringLiteral("-hide_banner"), QStringLiteral("-encoders")});
        if (!process.waitForStarted(5000) || !process.waitForFinished(10000) || process.exitCode() != 0) {
            return QStringLiteral("mpeg4");
        }
        const QString output = QString::fromUtf8(process.readAllStandardOutput() + process.readAllStandardError());
        const QStringList preferred{QStringLiteral("libx264"), QStringLiteral("libopenh264"), QStringLiteral("mpeg4")};
        for (const QString &encoder : preferred) {
            for (const QStringView line : QStringView(output).split(QLatin1Char('\n'))) {
                const QStringList fields = line.trimmed().toString().split(QLatin1Char(' '), Qt::SkipEmptyParts);
                if (fields.size() >= 2 && fields[0].startsWith(QLatin1Char('V')) && fields[1] == encoder) {
                    return encoder;
                }
            }
        }
        return QStringLiteral("mpeg4");
    }();
    return selected;
}
}

VideoPathRecorder &VideoPathRecorder::instance()
{
    static VideoPathRecorder recorder;
    return recorder;
}

VideoPathRecorder::VideoPathRecorder()
    : m_sessionId(qEnvironmentVariable("KDENLIVE_VIDEO_PATH_SESSION_ID"))
{
    bool pendingLimitValid = false;
    const int configuredPendingLimit = qEnvironmentVariableIntValue("KDENLIVE_VIDEO_PATH_MAX_PENDING_SIDECARS", &pendingLimitValid);
    if (pendingLimitValid) {
        m_maxPendingStateSidecars = qBound(1, configuredPendingLimit, 8);
    }
    m_stateSidecarPool.setMaxThreadCount(1);
    m_stateSidecarPool.setExpiryTimeout(30000);
    if (m_sessionId.isEmpty()) {
        m_sessionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    }
    const QString path = qEnvironmentVariable("KDENLIVE_VIDEO_PATH_LOG");
    if (path.isEmpty()) {
        return;
    }
    m_file = std::make_unique<QFile>(path);
    if (!m_file->open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text)) {
        m_file.reset();
        return;
    }
    const QFileInfo logInfo(path);
    m_logDirectory = logInfo.absolutePath();
    m_stateDirectory = qEnvironmentVariable("KDENLIVE_VIDEO_PATH_STATE_DIR");
    if (m_stateDirectory.isEmpty()) {
        m_stateDirectory = QDir(m_logDirectory).filePath(QStringLiteral("%1-states").arg(logInfo.completeBaseName()));
    }
    if (!QDir().mkpath(m_stateDirectory)) {
        m_file->close();
        m_file.reset();
        return;
    }
    m_entityMapPath = qEnvironmentVariable("KDENLIVE_VIDEO_PATH_ENTITY_MAP");
    if (m_entityMapPath.isEmpty()) {
        m_entityMapPath = QDir(m_logDirectory).filePath(QStringLiteral("entity-map.json"));
    }
    QFile entityMap(m_entityMapPath);
    if (entityMap.open(QIODevice::ReadOnly)) {
        const QJsonDocument document = QJsonDocument::fromJson(entityMap.readAll());
        const QJsonObject values = document.object();
        for (auto iterator = values.constBegin(); iterator != values.constEnd(); ++iterator) {
            if (iterator.value().isString()) {
                m_stableEntityIds.insert(iterator.key(), iterator.value().toString());
            }
        }
    }
    writeSessionStart();
}

VideoPathRecorder::~VideoPathRecorder()
{
    if (m_file && m_file->isOpen() && !m_sessionEnded) {
        flushPendingActions(false);
        recordLifecycle(QStringLiteral("session.abort"), QStringLiteral("recorder.destroyed_without_session_end"));
        waitForStateSidecars();
    }
}

bool VideoPathRecorder::isEnabled() const
{
    QMutexLocker locker(&m_mutex);
    return m_file && m_file->isOpen();
}

void VideoPathRecorder::beginTransaction(const QString &boundary, const QString &label, const QString &undoEntryKey)
{
    if (!isEnabled()) {
        return;
    }
    m_transactionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    m_transactionBoundary = boundary;
    m_transactionLabel = label;
    bindTransactionUndoEntry(undoEntryKey);
    // QAction is emitted before many Kdenlive undo commands are constructed.
    // Bind the queued command to this exact transaction once its ID exists;
    // never infer this relationship from elapsed time.
    for (QJsonObject &command : m_pendingCommands) {
        command.insert(QStringLiteral("transaction_id"), m_transactionId);
        if (!m_undoEntryId.isEmpty()) command.insert(QStringLiteral("undo_entry_id"), m_undoEntryId);
        writeEvent(command);
    }
    m_pendingCommands.clear();
}

void VideoPathRecorder::bindTransactionUndoEntry(const QString &undoEntryKey)
{
    if (undoEntryKey.isEmpty()) {
        m_undoEntryId.clear();
        m_targetTransactionId.clear();
        return;
    }
    m_undoEntryId = m_undoEntryIds.value(undoEntryKey);
    if (m_undoEntryId.isEmpty()) {
        m_undoEntryId = QUuid::createUuid().toString(QUuid::WithoutBraces);
        m_undoEntryIds.insert(undoEntryKey, m_undoEntryId);
    }
    m_targetTransactionId.clear();
    if (m_transactionBoundary == QLatin1String("commit") && m_commitTransactions.contains(m_undoEntryId)) {
        // QUndoStack merged this push into the retained command. All diffs in
        // that undo-stack entry deliberately share one transaction ID.
        m_transactionId = m_commitTransactions.value(m_undoEntryId);
    } else if (m_transactionBoundary == QLatin1String("undo") || m_transactionBoundary == QLatin1String("redo")) {
        m_targetTransactionId = m_commitTransactions.value(m_undoEntryId);
    }
}

void VideoPathRecorder::forgetUndoEntry(const QString &undoEntryKey)
{
    const QString undoEntryId = m_undoEntryIds.take(undoEntryKey);
    if (!undoEntryId.isEmpty()) {
        m_commitTransactions.remove(undoEntryId);
    }
}

void VideoPathRecorder::resetUndoEntries()
{
    m_undoEntryIds.clear();
    m_commitTransactions.clear();
}

void VideoPathRecorder::endTransaction()
{
    if (m_transactionId.isEmpty()) {
        return;
    }
    flushPendingActions(true);
    if (m_transactionBoundary == QLatin1String("commit")) {
        m_commitTransactions.insert(m_undoEntryId, m_transactionId);
    }
    m_lastCompletedTransactionId = m_transactionId;
    m_lastCompletedUndoEntryId = m_undoEntryId;
    m_lastCompletedTransaction.restart();
    m_transactionId.clear();
    m_transactionBoundary.clear();
    m_transactionLabel.clear();
    m_undoEntryId.clear();
    m_targetTransactionId.clear();
}

void VideoPathRecorder::setProjectStateProvider(std::function<QByteArray()> provider)
{
    m_projectStateProvider = std::move(provider);
}

void VideoPathRecorder::recordProjectContext(const QJsonObject &context)
{
    if (!isEnabled() || m_projectContextRecorded) {
        return;
    }
    QJsonObject event;
    event.insert(QStringLiteral("event_type"), QStringLiteral("project.context"));
    event.insert(QStringLiteral("context"), context);
    writeEvent(event);
    m_projectContextRecorded = true;
}

void VideoPathRecorder::recordLifecycle(const QString &eventType, const QString &reason, const QJsonObject &details)
{
    if (!isEnabled() || (eventType != QLatin1String("session.abort") && eventType != QLatin1String("session.recovered"))) {
        return;
    }
    QJsonObject event;
    event.insert(QStringLiteral("event_type"), eventType);
    event.insert(QStringLiteral("reason"), reason);
    if (!details.isEmpty()) {
        event.insert(QStringLiteral("details"), details);
    }
    writeEvent(event);
}

void VideoPathRecorder::addTransactionFields(QJsonObject &event, bool allowLastCompleted) const
{
    QString transactionId = m_transactionId;
    QString undoEntryId = m_undoEntryId;
    if (transactionId.isEmpty() && allowLastCompleted && m_lastCompletedTransaction.isValid() && m_lastCompletedTransaction.elapsed() < 2000) {
        transactionId = m_lastCompletedTransactionId;
        undoEntryId = m_lastCompletedUndoEntryId;
    }
    if (!transactionId.isEmpty()) {
        event.insert(QStringLiteral("transaction_id"), transactionId);
    }
    if (!undoEntryId.isEmpty()) {
        event.insert(QStringLiteral("undo_entry_id"), undoEntryId);
    }
    if (!m_targetTransactionId.isEmpty()) {
        event.insert(QStringLiteral("target_transaction_id"), m_targetTransactionId);
    }
}

QString VideoPathRecorder::stableEntityId(const QString &kind, const QString &nativeId) const
{
    const QString key = QStringLiteral("%1:%2").arg(kind, nativeId);
    auto iterator = m_stableEntityIds.constFind(key);
    if (iterator != m_stableEntityIds.constEnd()) {
        return iterator.value();
    }
    const QString value = QUuid::createUuid().toString(QUuid::WithoutBraces);
    m_stableEntityIds.insert(key, value);
    persistEntityMap();
    return value;
}

void VideoPathRecorder::persistEntityMap() const
{
    if (m_entityMapPath.isEmpty()) {
        return;
    }
    QJsonObject values;
    for (auto iterator = m_stableEntityIds.constBegin(); iterator != m_stableEntityIds.constEnd(); ++iterator) {
        values.insert(iterator.key(), iterator.value());
    }
    QDir().mkpath(QFileInfo(m_entityMapPath).absolutePath());
    QSaveFile output(m_entityMapPath);
    const QByteArray encoded = QJsonDocument(values).toJson(QJsonDocument::Indented);
    if (output.open(QIODevice::WriteOnly) && output.write(encoded) == encoded.size()) {
        output.commit();
    }
}

void VideoPathRecorder::initialize(QApplication *application)
{
    if (!isEnabled() || !application) {
        return;
    }
    application->installEventFilter(this);
    connect(application, &QCoreApplication::aboutToQuit, this, &VideoPathRecorder::writeSessionEnd);
    scheduleActionDiscovery();
}

void VideoPathRecorder::recordAction(const QString &action, const QString &timelineId, const QJsonObject &parameters)
{
    QJsonObject event;
    event.insert(QStringLiteral("event_type"), QStringLiteral("action"));
    event.insert(QStringLiteral("action"), action);
    event.insert(QStringLiteral("timeline_id"), timelineId);
    event.insert(QStringLiteral("parameters"), parameters);
    // Some operations report their semantic action immediately after the
    // undo-stack push has completed. Write those actions now: buffering until
    // the next transaction could overwrite the transaction attribution.
    if (m_transactionId.isEmpty() && m_lastCompletedTransaction.isValid() && m_lastCompletedTransaction.elapsed() < 2000) {
        addTransactionFields(event, true);
        writeEvent(event);
        return;
    }
    m_pendingActions.append(event);
}

void VideoPathRecorder::flushPendingActions(bool attachTransaction)
{
    for (QJsonObject &event : m_pendingActions) {
        if (attachTransaction) {
            addTransactionFields(event);
        }
        writeEvent(event);
    }
    m_pendingActions.clear();
}

void VideoPathRecorder::recordHistory(const QString &operation, const QString &label)
{
    QJsonObject event;
    event.insert(QStringLiteral("event_type"), QStringLiteral("history"));
    event.insert(QStringLiteral("action"), QStringLiteral("history.%1").arg(operation));
    event.insert(QStringLiteral("label"), label);
    addTransactionFields(event);
    if (m_lastShortcut.isValid() && m_lastShortcut.elapsed() < 1000 && !m_lastInputInteractionId.isEmpty()) {
        event.insert(QStringLiteral("interaction_id"), m_lastInputInteractionId);
    }
    writeEvent(event);
}

QJsonObject VideoPathRecorder::currentTimelineSnapshot() const
{
    if (!pCore || !pCore->window() || !pCore->window()->getCurrentTimeline()) {
        return {};
    }
    const auto model = pCore->window()->getCurrentTimeline()->model();
    if (!model) {
        return {};
    }

    QJsonArray tracks;
    for (int position = 0; position < model->getTracksCount(); ++position) {
        const int trackId = model->getTrackIndexFromPosition(position);
        QJsonObject track;
        track.insert(QStringLiteral("native_id"), trackId);
        track.insert(QStringLiteral("entity_id"), stableEntityId(QStringLiteral("track"), QString::number(trackId)));
        track.insert(QStringLiteral("position"), position);
        track.insert(QStringLiteral("kind"), model->isAudioTrack(trackId) ? QStringLiteral("audio") : QStringLiteral("video"));
        track.insert(QStringLiteral("tag"), model->getTrackTagById(trackId));
        track.insert(QStringLiteral("locked"), model->trackIsLocked(trackId));
        track.insert(QStringLiteral("muted"), model->trackIsMuted(trackId));
        track.insert(QStringLiteral("hidden"), model->trackIsHidden(trackId));
        track.insert(QStringLiteral("effects"), canonicalEffectStack(model->getTrackEffectStackModel(trackId)));
        tracks.append(track);
    }

    QJsonArray clips;
    QJsonArray compositions;
    QJsonArray mixes;
    auto items = model->getItemsInRange(-1, 0, -1, true);
    QList<int> itemIds(items.begin(), items.end());
    std::sort(itemIds.begin(), itemIds.end(), [model](int left, int right) {
        const int leftTrack = model->getItemTrackId(left);
        const int rightTrack = model->getItemTrackId(right);
        if (leftTrack != rightTrack) {
            return leftTrack < rightTrack;
        }
        const int leftPosition = model->getItemPosition(left);
        const int rightPosition = model->getItemPosition(right);
        return leftPosition == rightPosition ? left < right : leftPosition < rightPosition;
    });
    for (int itemId : itemIds) {
        if (model->isClip(itemId)) {
            const auto inOut = model->getClipInOut(itemId);
            const auto state = model->getClipState(itemId);
            QJsonObject clip;
            clip.insert(QStringLiteral("native_id"), itemId);
            clip.insert(QStringLiteral("entity_id"), stableEntityId(QStringLiteral("clip"), QString::number(itemId)));
            const QString assetReference = model->getClipBinId(itemId);
            clip.insert(QStringLiteral("asset_reference"), assetReference);
            clip.insert(QStringLiteral("asset_id"), stableEntityId(QStringLiteral("asset"), assetReference));
            clip.insert(QStringLiteral("track_native_id"), model->getClipTrackId(itemId));
            clip.insert(QStringLiteral("timeline_start_frame"), model->getClipPosition(itemId));
            clip.insert(QStringLiteral("duration_frames"), model->getClipPlaytime(itemId));
            clip.insert(QStringLiteral("source_start_frame"), inOut.first);
            clip.insert(QStringLiteral("source_end_frame"), inOut.second);
            clip.insert(QStringLiteral("speed"), model->getClipSpeed(itemId));
            clip.insert(QStringLiteral("clip_state"), int(state.first));
            clip.insert(QStringLiteral("clip_type"), int(state.second));
            clip.insert(QStringLiteral("effects"), canonicalEffectStack(model->getClipEffectStackModel(itemId)));
            clips.append(clip);
            if (model->getMixDuration(itemId) > 0) {
                QDomDocument document;
                const QDomElement mixElement = model->getMixXml(document, itemId);
                if (!mixElement.isNull()) {
                    QJsonObject mix;
                    mix.insert(QStringLiteral("native_id"), itemId);
                    mix.insert(QStringLiteral("entity_id"), stableEntityId(QStringLiteral("mix"), QString::number(itemId)));
                    mix.insert(QStringLiteral("track_native_id"), model->getClipTrackId(itemId));
                    mix.insert(QStringLiteral("parameters"), canonicalXmlElement(mixElement));
                    mixes.append(mix);
                }
            }
        } else if (model->isComposition(itemId)) {
            QJsonObject composition;
            composition.insert(QStringLiteral("native_id"), itemId);
            composition.insert(QStringLiteral("entity_id"), stableEntityId(QStringLiteral("composition"), QString::number(itemId)));
            composition.insert(QStringLiteral("track_native_id"), model->getCompositionTrackId(itemId));
            composition.insert(QStringLiteral("timeline_start_frame"), model->getCompositionPosition(itemId));
            composition.insert(QStringLiteral("duration_frames"), model->getCompositionPlaytime(itemId));
            const auto parameters = model->getCompositionParameterModel(itemId);
            if (parameters) {
                composition.insert(QStringLiteral("asset_id"), parameters->getAssetId());
                composition.insert(QStringLiteral("parameters"), parameters->toJson().object());
            }
            compositions.append(composition);
        }
    }

    QJsonObject snapshot;
    snapshot.insert(QStringLiteral("timeline_id"), model->uuid().toString(QUuid::WithoutBraces));
    snapshot.insert(QStringLiteral("duration_frames"), model->duration());
    snapshot.insert(QStringLiteral("tracks"), tracks);
    snapshot.insert(QStringLiteral("clips"), clips);
    snapshot.insert(QStringLiteral("compositions"), compositions);
    snapshot.insert(QStringLiteral("mixes"), mixes);
    QJsonObject masterEffects;
    masterEffects.insert(QStringLiteral("native_id"), 0);
    masterEffects.insert(QStringLiteral("entity_id"), stableEntityId(QStringLiteral("master_effect"), QStringLiteral("0")));
    masterEffects.insert(QStringLiteral("effects"), canonicalEffectStack(model->getMasterEffectStackModel()));
    snapshot.insert(QStringLiteral("master_effects"), QJsonArray{masterEffects});
    return snapshot;
}

QJsonObject VideoPathRecorder::projectStateReference(QByteArray *rawState)
{
    if (!m_projectStateProvider) {
        return {};
    }
    const QByteArray state = m_projectStateProvider();
    if (state.isEmpty()) {
        return {};
    }
    if (rawState) {
        *rawState = state;
    }
    const QString digest = QString::fromLatin1(QCryptographicHash::hash(state, QCryptographicHash::Sha256).toHex());
#ifdef VIDEOPATH_HAVE_ZSTD
    const QString encoding = QStringLiteral("zstd");
    const QString extension = QStringLiteral(".kdenlive.zst");
#else
    const QString encoding = QStringLiteral("qt-qcompress");
    const QString extension = QStringLiteral(".kdenlive.qz");
#endif
    const QString target = QDir(m_stateDirectory).filePath(digest + extension);
    const QString relative = QDir(m_logDirectory).relativeFilePath(target);
    if (!m_scheduledStateHashes.contains(digest) && !QFileInfo::exists(target)) {
        reapFinishedStateSidecars(true);
        m_scheduledStateHashes.insert(digest);
        PendingSidecarWrite pending;
        pending.stateHash = digest;
        pending.future = QtConcurrent::run(&m_stateSidecarPool, [state, target]() -> bool {
            QByteArray compressed;
#ifdef VIDEOPATH_HAVE_ZSTD
            const size_t bound = ZSTD_compressBound(static_cast<size_t>(state.size()));
            compressed.resize(static_cast<qsizetype>(bound));
            const size_t written = ZSTD_compress(compressed.data(), bound, state.constData(), static_cast<size_t>(state.size()), 3);
            if (ZSTD_isError(written)) {
                return false;
            }
            compressed.resize(static_cast<qsizetype>(written));
#else
            compressed = qCompress(state, 9);
#endif
            QSaveFile output(target);
            if (!output.open(QIODevice::WriteOnly) || output.write(compressed) != compressed.size()) {
                return false;
            }
            return output.commit();
        });
        m_stateSidecarWrites.append(std::move(pending));
    }
    QJsonObject reference;
    reference.insert(QStringLiteral("encoding"), encoding);
    reference.insert(QStringLiteral("path"), relative);
    reference.insert(QStringLiteral("sha256"), digest);
    reference.insert(QStringLiteral("bytes"), state.size());
    reference.insert(QStringLiteral("durability"), QStringLiteral("complete_on_session_end"));
    return reference;
}

QJsonObject VideoPathRecorder::scheduleCheckpointProxy(const QByteArray &projectState)
{
    if (projectState.isEmpty() || qEnvironmentVariable("KDENLIVE_VIDEO_PATH_CHECKPOINT_PROXIES") == QLatin1String("0")) {
        return {};
    }
    QDomDocument document;
    if (!document.setContent(projectState)) {
        return {};
    }
    const QDomElement profile = document.documentElement().firstChildElement(QStringLiteral("profile"));
    const int sourceWidth = profile.attribute(QStringLiteral("width")).toInt();
    const int sourceHeight = profile.attribute(QStringLiteral("height")).toInt();
    if (sourceWidth <= 0 || sourceHeight <= 0) {
        return {};
    }
    const int width = std::min(sourceWidth, 640);
    int height = qRound(double(sourceHeight) * width / sourceWidth);
    height = std::max(2, height - (height % 2));
    const QString digest = QString::fromLatin1(QCryptographicHash::hash(projectState, QCryptographicHash::Sha256).toHex());
    const QString proxyDirectory = QDir(m_stateDirectory).filePath(QStringLiteral("checkpoint_refs"));
    if (!QDir().mkpath(proxyDirectory)) {
        return {};
    }
    const QString target = QDir(proxyDirectory).filePath(digest + QStringLiteral(".mp4"));
    if (!m_scheduledCheckpointHashes.contains(digest) && !QFileInfo::exists(target)) {
        reapFinishedStateSidecars(true);
        m_scheduledCheckpointHashes.insert(digest);
        const QString videoEncoder = checkpointVideoEncoder();
        PendingSidecarWrite pending;
        pending.checkpointHash = digest;
        pending.future = QtConcurrent::run(&m_stateSidecarPool, [projectState, target, width, height, videoEncoder]() -> bool {
            const QString token = QUuid::createUuid().toString(QUuid::WithoutBraces);
            const QString temporaryProject = QFileInfo(target).absolutePath() + QStringLiteral("/.%1.kdenlive").arg(token);
            const QString temporaryVideo = QFileInfo(target).absolutePath() + QStringLiteral("/.%1.mp4").arg(token);
            QSaveFile projectFile(temporaryProject);
            if (!projectFile.open(QIODevice::WriteOnly) || projectFile.write(projectState) != projectState.size() || !projectFile.commit()) {
                return false;
            }
            const QString melt = qEnvironmentVariable("KDENLIVE_VIDEO_PATH_MELT", QStringLiteral("melt"));
            QStringList arguments{
                QStringLiteral("-progress"),
                temporaryProject,
                QStringLiteral("-consumer"),
                QStringLiteral("avformat:%1").arg(temporaryVideo),
                QStringLiteral("f=mp4"),
                QStringLiteral("vcodec=%1").arg(videoEncoder),
            };
            if (videoEncoder == QLatin1String("libx264")) {
                arguments << QStringLiteral("crf=28") << QStringLiteral("preset=ultrafast");
            } else if (videoEncoder == QLatin1String("libopenh264")) {
                arguments << QStringLiteral("vb=2M") << QStringLiteral("g=50");
            } else {
                arguments << QStringLiteral("qscale=3") << QStringLiteral("g=50");
            }
            arguments << QStringLiteral("acodec=aac") << QStringLiteral("ab=64k") << QStringLiteral("width=%1").arg(width)
                      << QStringLiteral("height=%1").arg(height) << QStringLiteral("rescale=bilinear");
            const int result = QProcess::execute(melt, arguments);
            QFile::remove(temporaryProject);
            if (result != 0 || !QFileInfo::exists(temporaryVideo)) {
                QFile::remove(temporaryVideo);
                return false;
            }
            if (QFileInfo::exists(target)) {
                QFile::remove(temporaryVideo);
                return true;
            }
            return QFile::rename(temporaryVideo, target);
        });
        m_stateSidecarWrites.append(std::move(pending));
    }
    QJsonObject reference;
    reference.insert(QStringLiteral("path"), QDir(m_logDirectory).relativeFilePath(target));
    reference.insert(QStringLiteral("base"), QStringLiteral("trajectory"));
    reference.insert(QStringLiteral("width"), width);
    reference.insert(QStringLiteral("height"), height);
    reference.insert(QStringLiteral("render_preset"), QStringLiteral("checkpoint-proxy-v1"));
    reference.insert(QStringLiteral("durability"), QStringLiteral("complete_on_session_end"));
    return reference;
}

void VideoPathRecorder::reapFinishedStateSidecars(bool waitForSlot)
{
    const auto finish = [this](int index) {
        PendingSidecarWrite pending = m_stateSidecarWrites.takeAt(index);
        pending.future.waitForFinished();
        const bool succeeded = pending.future.result();
        if (!pending.stateHash.isEmpty()) {
            m_scheduledStateHashes.remove(pending.stateHash);
            if (succeeded) {
                m_failedStateHashes.remove(pending.stateHash);
            } else {
                m_failedStateHashes.insert(pending.stateHash);
            }
        }
        if (!pending.checkpointHash.isEmpty()) {
            m_scheduledCheckpointHashes.remove(pending.checkpointHash);
            if (succeeded) {
                m_failedCheckpointHashes.remove(pending.checkpointHash);
            } else {
                m_failedCheckpointHashes.insert(pending.checkpointHash);
            }
        }
    };
    for (int index = m_stateSidecarWrites.size() - 1; index >= 0; --index) {
        if (m_stateSidecarWrites[index].future.isFinished()) {
            finish(index);
        }
    }
    while (waitForSlot && m_stateSidecarWrites.size() >= m_maxPendingStateSidecars) {
        finish(0);
    }
}

bool VideoPathRecorder::waitForStateSidecars()
{
    reapFinishedStateSidecars(false);
    while (!m_stateSidecarWrites.isEmpty()) {
        PendingSidecarWrite pending = m_stateSidecarWrites.takeFirst();
        pending.future.waitForFinished();
        const bool succeeded = pending.future.result();
        if (!pending.stateHash.isEmpty()) {
            m_scheduledStateHashes.remove(pending.stateHash);
            if (succeeded) {
                m_failedStateHashes.remove(pending.stateHash);
            } else {
                m_failedStateHashes.insert(pending.stateHash);
            }
        }
        if (!pending.checkpointHash.isEmpty()) {
            m_scheduledCheckpointHashes.remove(pending.checkpointHash);
            if (succeeded) {
                m_failedCheckpointHashes.remove(pending.checkpointHash);
            } else {
                m_failedCheckpointHashes.insert(pending.checkpointHash);
            }
        }
    }
    return m_failedStateHashes.isEmpty() && m_failedCheckpointHashes.isEmpty();
}

QJsonObject VideoPathRecorder::diffSnapshots(const QJsonObject &before, const QJsonObject &after)
{
    QJsonArray changes;
    const auto compareEntities = [&changes, &before, &after](const QString &entity, const QString &singular) {
        QHash<int, QJsonObject> previous;
        QHash<int, QJsonObject> current;
        for (const auto &value : before.value(entity).toArray()) {
            const auto object = value.toObject();
            previous.insert(object.value(QStringLiteral("native_id")).toInt(), object);
        }
        for (const auto &value : after.value(entity).toArray()) {
            const auto object = value.toObject();
            current.insert(object.value(QStringLiteral("native_id")).toInt(), object);
        }
        QList<int> ids = previous.keys();
        for (int id : current.keys()) {
            if (!ids.contains(id)) {
                ids.append(id);
            }
        }
        std::sort(ids.begin(), ids.end());
        for (int id : ids) {
            QJsonObject change;
            change.insert(QStringLiteral("entity"), singular);
            change.insert(QStringLiteral("native_id"), id);
            if (!previous.contains(id)) {
                change.insert(QStringLiteral("change"), QStringLiteral("added"));
                change.insert(QStringLiteral("after"), current.value(id));
            } else if (!current.contains(id)) {
                change.insert(QStringLiteral("change"), QStringLiteral("removed"));
                change.insert(QStringLiteral("before"), previous.value(id));
            } else if (previous.value(id) != current.value(id)) {
                change.insert(QStringLiteral("change"), QStringLiteral("updated"));
                change.insert(QStringLiteral("before"), previous.value(id));
                change.insert(QStringLiteral("after"), current.value(id));
            } else {
                continue;
            }
            changes.append(change);
        }
    };
    compareEntities(QStringLiteral("tracks"), QStringLiteral("track"));
    compareEntities(QStringLiteral("clips"), QStringLiteral("clip"));
    compareEntities(QStringLiteral("compositions"), QStringLiteral("composition"));
    compareEntities(QStringLiteral("mixes"), QStringLiteral("mix"));
    compareEntities(QStringLiteral("master_effects"), QStringLiteral("master_effect"));
    QJsonObject diff;
    diff.insert(QStringLiteral("changes"), changes);
    if (before.value(QStringLiteral("duration_frames")) != after.value(QStringLiteral("duration_frames"))) {
        diff.insert(QStringLiteral("duration_before"), before.value(QStringLiteral("duration_frames")));
        diff.insert(QStringLiteral("duration_after"), after.value(QStringLiteral("duration_frames")));
    }
    return diff;
}

bool VideoPathRecorder::captureTimelineCheckpoint(const QString &label)
{
    const QJsonObject snapshot = currentTimelineSnapshot();
    if (snapshot.isEmpty()) {
        return false;
    }
    const QByteArray canonical = QJsonDocument(snapshot).toJson(QJsonDocument::Compact);
    const QString timelineId = snapshot.value(QStringLiteral("timeline_id")).toString();
    m_lastSnapshots.insert(timelineId, snapshot);
    QJsonObject event;
    event.insert(QStringLiteral("event_type"), QStringLiteral("state.checkpoint"));
    event.insert(QStringLiteral("label"), label);
    event.insert(QStringLiteral("timeline_id"), timelineId);
    event.insert(QStringLiteral("state_hash"), QString::fromLatin1(QCryptographicHash::hash(canonical, QCryptographicHash::Sha256).toHex()));
    event.insert(QStringLiteral("snapshot"), snapshot);
    QByteArray rawProjectState;
    const QJsonObject projectState = projectStateReference(&rawProjectState);
    if (!projectState.isEmpty()) {
        event.insert(QStringLiteral("project_state"), projectState);
        m_lastProjectStateHash = projectState.value(QStringLiteral("sha256")).toString();
    }
    const QJsonObject referenceProxy = scheduleCheckpointProxy(rawProjectState);
    if (!referenceProxy.isEmpty()) {
        event.insert(QStringLiteral("reference_proxy"), referenceProxy);
    }
    addTransactionFields(event);
    flushPendingActions(true);
    writeEvent(event);
    return true;
}

void VideoPathRecorder::captureTimelineChange(const QString &label, const QString &boundary)
{
    const QJsonObject after = currentTimelineSnapshot();
    if (after.isEmpty()) {
        return;
    }
    const QString timelineId = after.value(QStringLiteral("timeline_id")).toString();
    if (!m_lastSnapshots.contains(timelineId)) {
        captureTimelineCheckpoint(QStringLiteral("late-baseline"));
        return;
    }
    const QJsonObject before = m_lastSnapshots.value(timelineId);
    const QByteArray beforeJson = QJsonDocument(before).toJson(QJsonDocument::Compact);
    const QByteArray afterJson = QJsonDocument(after).toJson(QJsonDocument::Compact);
    if (beforeJson == afterJson) {
        return;
    }
    QJsonObject event;
    event.insert(QStringLiteral("event_type"), QStringLiteral("state.diff"));
    event.insert(QStringLiteral("label"), label);
    event.insert(QStringLiteral("boundary"), boundary);
    event.insert(QStringLiteral("timeline_id"), timelineId);
    event.insert(QStringLiteral("before_hash"), QString::fromLatin1(QCryptographicHash::hash(beforeJson, QCryptographicHash::Sha256).toHex()));
    event.insert(QStringLiteral("after_hash"), QString::fromLatin1(QCryptographicHash::hash(afterJson, QCryptographicHash::Sha256).toHex()));
    const QJsonObject timelineDiff = diffSnapshots(before, after);
    event.insert(QStringLiteral("diff"), timelineDiff);
    const QJsonObject projectState = projectStateReference();
    if (!projectState.isEmpty()) {
        event.insert(QStringLiteral("project_state"), projectState);
        event.insert(QStringLiteral("project_before_hash"), m_lastProjectStateHash);
        event.insert(QStringLiteral("project_after_hash"), projectState.value(QStringLiteral("sha256")).toString());
    }
    addTransactionFields(event);
    flushPendingActions(true);
    if (m_lastInteraction.isValid() && m_lastInteraction.elapsed() < 2000 && !m_lastInputInteractionId.isEmpty()) {
        event.insert(QStringLiteral("interaction_id"), m_lastInputInteractionId);
    }
    // Effect-panel and curve-editor edits may not produce a QAction or recent
    // pointer event. Still give the state mutation an explicit correlation
    // identity so it cannot disappear into an unlinked raw diff.
    bool effectChange = false;
    bool keyframeChange = false;
    const QJsonArray changes = timelineDiff.value(QStringLiteral("changes")).toArray();
    for (const QJsonValue &value : changes) {
        const QJsonObject change = value.toObject();
        if (change.value(QStringLiteral("entity")).toString() != QStringLiteral("clip")) continue;
        const QJsonObject beforeClip = change.value(QStringLiteral("before")).toObject();
        const QJsonObject afterClip = change.value(QStringLiteral("after")).toObject();
        if (beforeClip.value(QStringLiteral("effects")) != afterClip.value(QStringLiteral("effects"))) {
            effectChange = true;
            const QString serialized = QJsonDocument(afterClip.value(QStringLiteral("effects")).toArray()).toJson(QJsonDocument::Compact);
            keyframeChange = serialized.contains('=');
            break;
        }
    }
    if (effectChange) {
        QString interactionId = event.value(QStringLiteral("interaction_id")).toString();
        const bool ambiguous = interactionId.isEmpty();
        if (interactionId.isEmpty()) interactionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
        event.insert(QStringLiteral("interaction_id"), interactionId);
        QJsonObject intent;
        intent.insert(QStringLiteral("kind"), keyframeChange ? QStringLiteral("keyframe.update") : QStringLiteral("effect.change"));
        intent.insert(QStringLiteral("interaction_id"), interactionId);
        intent.insert(QStringLiteral("ambiguous"), ambiguous);
        event.insert(QStringLiteral("intent"), intent);
    }
    writeEvent(event);
    if (!projectState.isEmpty()) {
        m_lastProjectStateHash = projectState.value(QStringLiteral("sha256")).toString();
    }
    m_lastSnapshots.insert(timelineId, after);
}

void VideoPathRecorder::scheduleActionDiscovery()
{
    if (m_actionDiscoveryScheduled) {
        return;
    }
    m_actionDiscoveryScheduled = true;
    QTimer::singleShot(0, this, [this]() {
        m_actionDiscoveryScheduled = false;
        attachActions();
    });
}

void VideoPathRecorder::attachActions()
{
    const auto widgets = QApplication::allWidgets();
    for (QWidget *widget : widgets) {
        const auto actions = widget->findChildren<QAction *>();
        for (QAction *action : actions) {
            if (m_actions.contains(action)) {
                continue;
            }
            m_actions.insert(action);
            connect(action, &QObject::destroyed, this, [this, action]() { m_actions.remove(action); });
            connect(action, &QAction::triggered, this, [this, action](bool checked) { recordCommand(action, checked); });
        }
    }
}

void VideoPathRecorder::recordCommand(QAction *action, bool checked)
{
    if (!action || action->isSeparator()) {
        return;
    }
    QString source = QStringLiteral("programmatic_or_unknown");
    QString interactionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    if (m_lastShortcut.isValid() && m_lastShortcut.elapsed() < 500) {
        source = QStringLiteral("keyboard");
        interactionId = m_lastInputInteractionId;
    } else if (m_lastMenuClick.isValid() && m_lastMenuClick.elapsed() < 500) {
        source = QStringLiteral("menu");
        interactionId = m_lastInputInteractionId;
    } else if (m_lastToolbarClick.isValid() && m_lastToolbarClick.elapsed() < 500) {
        source = QStringLiteral("toolbar");
        interactionId = m_lastInputInteractionId;
    }
    QString commandId = action->objectName();
    bool commandRegistered = !commandId.isEmpty();
    if (commandId.isEmpty()) {
        // QAction object names are absent for several dynamically-created
        // actions.  Keep a stable, reviewable ID instead of collapsing all of
        // them into the misleading literal "unmapped".
        QString scope;
        for (QObject *owner = action->parent(); owner; owner = owner->parent()) {
            if (!owner->objectName().isEmpty()) {
                scope = owner->objectName() + QLatin1Char('/') + scope;
            }
        }
        QString stableKey = scope + QLatin1Char('|') + action->text();
        stableKey.remove(QLatin1Char('&'));
        const QByteArray digest = QCryptographicHash::hash(stableKey.toUtf8(), QCryptographicHash::Sha256).toHex().left(16);
        commandId = QStringLiteral("generated.%1").arg(QString::fromLatin1(digest));
        commandRegistered = false;
    }
    QString label = action->text();
    label.remove(QLatin1Char('&'));
    QJsonArray shortcuts;
    for (const QKeySequence &shortcut : action->shortcuts()) {
        shortcuts.append(shortcut.toString(QKeySequence::PortableText));
    }
    QJsonObject event;
    event.insert(QStringLiteral("event_type"), QStringLiteral("ui.command"));
    event.insert(QStringLiteral("interaction_id"), interactionId);
    event.insert(QStringLiteral("command_id"), commandId);
    event.insert(QStringLiteral("command_registered"), commandRegistered);
    event.insert(QStringLiteral("label"), label);
    event.insert(QStringLiteral("source"), source);
    event.insert(QStringLiteral("checked"), checked);
    event.insert(QStringLiteral("shortcuts"), shortcuts);
    event.insert(QStringLiteral("focus"), describeObject(QApplication::focusWidget()));
    if (!m_transactionId.isEmpty()) {
        addTransactionFields(event);
        writeEvent(event);
        m_lastInputInteractionId = interactionId;
        m_lastInteraction.restart();
        return;
    }
    m_lastInputInteractionId = interactionId;
    m_lastInteraction.restart();
    // The undo transaction is created after QAction::triggered on several
    // Kdenlive paths. Queue the command until beginTransaction() can bind it
    // to the authoritative transaction ID.
    m_pendingCommands.append(event);
}

void VideoPathRecorder::recordShortcut(const QKeySequence &sequence, bool ambiguous)
{
    const QString portableSequence = sequence.toString(QKeySequence::PortableText);
    if (portableSequence.isEmpty()) {
        return;
    }
    if (m_lastShortcut.isValid() && m_lastShortcut.elapsed() < 100 && m_lastShortcutSequence == portableSequence) {
        return;
    }
    m_lastShortcut.restart();
    m_lastShortcutSequence = portableSequence;
    m_lastInputInteractionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    m_lastInteraction.restart();
    QJsonObject shortcutEvent;
    shortcutEvent.insert(QStringLiteral("event_type"), QStringLiteral("ui.shortcut"));
    shortcutEvent.insert(QStringLiteral("interaction_id"), m_lastInputInteractionId);
    shortcutEvent.insert(QStringLiteral("key_sequence"), portableSequence);
    shortcutEvent.insert(QStringLiteral("ambiguous"), ambiguous);
    shortcutEvent.insert(QStringLiteral("focus"), describeObject(QApplication::focusWidget()));
    writeEvent(shortcutEvent);
}

QString VideoPathRecorder::describeObject(const QObject *object)
{
    if (!object) {
        return QStringLiteral("none");
    }
    const QString name = object->objectName();
    return name.isEmpty() ? QString::fromLatin1(object->metaObject()->className())
                          : QStringLiteral("%1#%2").arg(QString::fromLatin1(object->metaObject()->className()), name);
}

bool VideoPathRecorder::isTimelineCanvasTarget(const QObject *object)
{
    if (!object || !QString::fromLatin1(object->metaObject()->className()).contains(QStringLiteral("QQuick"))) {
        return false;
    }
    for (const QObject *current = object; current; current = current->parent()) {
        if (describeObject(current).contains(QStringLiteral("timeline"), Qt::CaseInsensitive)) {
            return true;
        }
    }
    return false;
}

bool VideoPathRecorder::hasMenuAncestor(const QObject *object)
{
    for (const QObject *current = object; current; current = current->parent()) {
        if (qobject_cast<const QMenu *>(current)) {
            return true;
        }
    }
    return false;
}

bool VideoPathRecorder::hasToolButtonAncestor(const QObject *object)
{
    for (const QObject *current = object; current; current = current->parent()) {
        if (qobject_cast<const QToolButton *>(current)) {
            return true;
        }
    }
    return false;
}

bool VideoPathRecorder::eventFilter(QObject *watched, QEvent *event)
{
    if (event->type() == QEvent::ChildAdded) {
        scheduleActionDiscovery();
    } else if (event->type() == QEvent::KeyPress) {
        const auto *key = static_cast<QKeyEvent *>(event);
        const bool isModifierOnly = key->key() == Qt::Key_Control || key->key() == Qt::Key_Shift || key->key() == Qt::Key_Alt || key->key() == Qt::Key_Meta;
        const bool hasShortcutModifier = key->modifiers().testAnyFlags(Qt::ControlModifier | Qt::AltModifier | Qt::MetaModifier);
        const bool isFunctionKey = key->key() >= Qt::Key_F1 && key->key() <= Qt::Key_F35;
        if (!key->isAutoRepeat() && !isModifierOnly && (hasShortcutModifier || isFunctionKey)) {
            recordShortcut(QKeySequence(QKeyCombination(key->modifiers(), Qt::Key(key->key()))), false);
        }
    } else if (event->type() == QEvent::Shortcut) {
        const auto *shortcut = static_cast<QShortcutEvent *>(event);
        recordShortcut(shortcut->key(), shortcut->isAmbiguous());
    } else if (event->type() == QEvent::MouseButtonPress && hasMenuAncestor(watched)) {
        m_lastMenuClick.restart();
        m_lastInputInteractionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    } else if (event->type() == QEvent::MouseButtonPress && hasToolButtonAncestor(watched)) {
        m_lastToolbarClick.restart();
        m_lastInputInteractionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    }

    if (isTimelineCanvasTarget(watched) && (event->type() == QEvent::MouseButtonPress || event->type() == QEvent::MouseButtonRelease)) {
        const auto *mouse = static_cast<QMouseEvent *>(event);
        if (event->type() == QEvent::MouseButtonPress) {
            m_pointerInteractionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
            m_pointerStart = mouse->globalPosition();
            m_pointerTarget = describeObject(watched);
            m_pointerButton = int(mouse->button());
        } else if (!m_pointerInteractionId.isEmpty()) {
            QJsonObject start{{QStringLiteral("x"), m_pointerStart.x()}, {QStringLiteral("y"), m_pointerStart.y()}};
            QJsonObject end{{QStringLiteral("x"), mouse->globalPosition().x()}, {QStringLiteral("y"), mouse->globalPosition().y()}};
            QJsonObject gesture;
            gesture.insert(QStringLiteral("event_type"), QStringLiteral("ui.gesture"));
            gesture.insert(QStringLiteral("interaction_id"), m_pointerInteractionId);
            gesture.insert(QStringLiteral("gesture"), m_pointerStart == mouse->globalPosition() ? QStringLiteral("click") : QStringLiteral("drag"));
            gesture.insert(QStringLiteral("target"), m_pointerTarget);
            gesture.insert(QStringLiteral("button"), m_pointerButton);
            gesture.insert(QStringLiteral("start_global"), start);
            gesture.insert(QStringLiteral("end_global"), end);
            gesture.insert(QStringLiteral("modifiers"), int(mouse->modifiers()));
            m_lastInputInteractionId = m_pointerInteractionId;
            m_lastInteraction.restart();
            writeEvent(gesture);
            m_pointerInteractionId.clear();
        }
    }
    return QObject::eventFilter(watched, event);
}

void VideoPathRecorder::writeEvent(QJsonObject event)
{
    QMutexLocker locker(&m_mutex);
    if (!m_file || !m_file->isOpen()) {
        return;
    }
    event.insert(QStringLiteral("schema_version"), QStringLiteral("0.3.0"));
    event.insert(QStringLiteral("session_id"), m_sessionId);
    event.insert(QStringLiteral("sequence"), ++m_sequence);
    event.insert(QStringLiteral("event_id"), QUuid::createUuid().toString(QUuid::WithoutBraces));
    event.insert(QStringLiteral("timestamp_utc"), QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs));
    m_file->write(QJsonDocument(event).toJson(QJsonDocument::Compact));
    m_file->write("\n");
    m_file->flush();
}

void VideoPathRecorder::writeSessionStart()
{
    QJsonObject event;
    event.insert(QStringLiteral("event_type"), QStringLiteral("session.start"));
    event.insert(QStringLiteral("schema_version"), QStringLiteral("0.3.0"));
    event.insert(QStringLiteral("session_id"), m_sessionId);
    event.insert(QStringLiteral("sequence"), ++m_sequence);
    event.insert(QStringLiteral("event_id"), QUuid::createUuid().toString(QUuid::WithoutBraces));
    event.insert(QStringLiteral("timestamp_utc"), QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs));
    event.insert(QStringLiteral("application"), QStringLiteral("kdenlive-video-path-pilot"));
    const QString applicationVersion = QCoreApplication::applicationVersion().isEmpty() ? QString::fromLatin1(KDENLIVE_VERSION)
                                                                                         : QCoreApplication::applicationVersion();
    event.insert(QStringLiteral("application_version"), applicationVersion);
    event.insert(QStringLiteral("os"), QSysInfo::prettyProductName());
    event.insert(QStringLiteral("cpu_architecture"), QSysInfo::currentCpuArchitecture());
    const QString segment = qEnvironmentVariable("KDENLIVE_VIDEO_PATH_SEGMENT");
    if (!segment.isEmpty()) {
        event.insert(QStringLiteral("segment"), segment.toInt());
    }
    m_file->write(QJsonDocument(event).toJson(QJsonDocument::Compact));
    m_file->write("\n");
    m_file->flush();
}

void VideoPathRecorder::writeSessionEnd()
{
    if (m_sessionEnded) {
        return;
    }
    m_sessionEnded = true;
    flushPendingActions(false);
    captureTimelineCheckpoint(QStringLiteral("session.final"));
    QJsonObject event;
    event.insert(QStringLiteral("event_type"), QStringLiteral("session.end"));
    event.insert(QStringLiteral("reason"), QStringLiteral("application.quit"));
    event.insert(QStringLiteral("state_sidecars_complete"), waitForStateSidecars());
    writeEvent(event);
}
