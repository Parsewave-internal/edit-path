/*
    SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
    SPDX-License-Identifier: GPL-3.0-only
*/

#include "videopathrecorder.hpp"

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
#include <QDomDocument>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QKeyEvent>
#include <QKeySequence>
#include <QMenu>
#include <QMouseEvent>
#include <QMutexLocker>
#include <QShortcutEvent>
#include <QSysInfo>
#include <QTimer>
#include <QToolButton>
#include <QUuid>
#include <QWidget>

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
}

VideoPathRecorder &VideoPathRecorder::instance()
{
    static VideoPathRecorder recorder;
    return recorder;
}

VideoPathRecorder::VideoPathRecorder()
    : m_sessionId(QUuid::createUuid().toString(QUuid::WithoutBraces))
{
    const QString path = qEnvironmentVariable("KDENLIVE_VIDEO_PATH_LOG");
    if (path.isEmpty()) {
        return;
    }
    m_file = std::make_unique<QFile>(path);
    if (!m_file->open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text)) {
        m_file.reset();
        return;
    }
    writeSessionStart();
}

VideoPathRecorder::~VideoPathRecorder() = default;

bool VideoPathRecorder::isEnabled() const
{
    QMutexLocker locker(&m_mutex);
    return m_file && m_file->isOpen();
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
    writeEvent(event);
}

void VideoPathRecorder::recordHistory(const QString &operation, const QString &label)
{
    QJsonObject event;
    event.insert(QStringLiteral("event_type"), QStringLiteral("history"));
    event.insert(QStringLiteral("action"), QStringLiteral("history.%1").arg(operation));
    event.insert(QStringLiteral("label"), label);
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
        track.insert(QStringLiteral("position"), position);
        track.insert(QStringLiteral("kind"), model->isAudioTrack(trackId) ? QStringLiteral("audio") : QStringLiteral("video"));
        track.insert(QStringLiteral("tag"), model->getTrackTagById(trackId));
        track.insert(QStringLiteral("locked"), model->trackIsLocked(trackId));
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
            clip.insert(QStringLiteral("asset_reference"), model->getClipBinId(itemId));
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
                    mix.insert(QStringLiteral("track_native_id"), model->getClipTrackId(itemId));
                    mix.insert(QStringLiteral("parameters"), canonicalXmlElement(mixElement));
                    mixes.append(mix);
                }
            }
        } else if (model->isComposition(itemId)) {
            QJsonObject composition;
            composition.insert(QStringLiteral("native_id"), itemId);
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
    masterEffects.insert(QStringLiteral("effects"), canonicalEffectStack(model->getMasterEffectStackModel()));
    snapshot.insert(QStringLiteral("master_effects"), QJsonArray{masterEffects});
    return snapshot;
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

void VideoPathRecorder::captureTimelineCheckpoint(const QString &label)
{
    const QJsonObject snapshot = currentTimelineSnapshot();
    if (snapshot.isEmpty()) {
        return;
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
    writeEvent(event);
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
    event.insert(QStringLiteral("diff"), diffSnapshots(before, after));
    if (m_lastInteraction.isValid() && m_lastInteraction.elapsed() < 2000 && !m_lastInputInteractionId.isEmpty()) {
        event.insert(QStringLiteral("interaction_id"), m_lastInputInteractionId);
    }
    writeEvent(event);
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
    if (commandId.isEmpty()) {
        commandId = QStringLiteral("unmapped");
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
    event.insert(QStringLiteral("label"), label);
    event.insert(QStringLiteral("source"), source);
    event.insert(QStringLiteral("checked"), checked);
    event.insert(QStringLiteral("shortcuts"), shortcuts);
    event.insert(QStringLiteral("focus"), describeObject(QApplication::focusWidget()));
    m_lastInputInteractionId = interactionId;
    m_lastInteraction.restart();
    writeEvent(event);
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
    event.insert(QStringLiteral("schema_version"), QStringLiteral("0.2.0"));
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
    event.insert(QStringLiteral("schema_version"), QStringLiteral("0.2.0"));
    event.insert(QStringLiteral("session_id"), m_sessionId);
    event.insert(QStringLiteral("sequence"), ++m_sequence);
    event.insert(QStringLiteral("event_id"), QUuid::createUuid().toString(QUuid::WithoutBraces));
    event.insert(QStringLiteral("timestamp_utc"), QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs));
    event.insert(QStringLiteral("application"), QStringLiteral("kdenlive-video-path-pilot"));
    event.insert(QStringLiteral("application_version"), QCoreApplication::applicationVersion());
    event.insert(QStringLiteral("os"), QSysInfo::prettyProductName());
    event.insert(QStringLiteral("cpu_architecture"), QSysInfo::currentCpuArchitecture());
    m_file->write(QJsonDocument(event).toJson(QJsonDocument::Compact));
    m_file->write("\n");
    m_file->flush();
}

void VideoPathRecorder::writeSessionEnd()
{
    QJsonObject event;
    event.insert(QStringLiteral("event_type"), QStringLiteral("session.end"));
    event.insert(QStringLiteral("reason"), QStringLiteral("application.quit"));
    writeEvent(event);
}
