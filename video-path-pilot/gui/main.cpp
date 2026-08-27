// SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
// SPDX-License-Identifier: GPL-3.0-only

#include <QApplication>
#include <QClipboard>
#include <QCloseEvent>
#include <QDateTime>
#include <QDesktopServices>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLibraryInfo>
#include <QMainWindow>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QProcess>
#include <QProcessEnvironment>
#include <QProgressBar>
#include <QPushButton>
#include <QSaveFile>
#include <QSettings>
#include <QStandardPaths>
#include <QTemporaryDir>
#include <QTextStream>
#include <QTimer>
#include <QUrl>
#include <QUuid>
#include <QVBoxLayout>

#include <utility>

namespace {
QString repositoryRoot()
{
    const QString configured = qEnvironmentVariable("EDIT_PATH_REPO_ROOT");
    if (!configured.isEmpty() && QFileInfo::exists(configured + QStringLiteral("/video-path-pilot/job_pipeline.py"))) return QDir(configured).absolutePath();
    QDir current(QCoreApplication::applicationDirPath());
    for (int depth = 0; depth < 6; ++depth) {
        if (QFileInfo::exists(current.filePath(QStringLiteral("video-path-pilot/job_pipeline.py")))) return current.absolutePath();
        if (!current.cdUp()) break;
    }
    return {};
}

QString pythonExecutable()
{
#ifdef Q_OS_WIN
    const QString bundled = QDir(QCoreApplication::applicationDirPath()).filePath(QStringLiteral("python/python.exe"));
    if (QFileInfo::exists(bundled)) return bundled;
    return QStringLiteral("python.exe");
#else
    return QStringLiteral("python3");
#endif
}

QString sessionsRoot()
{
    QString videos = QStandardPaths::writableLocation(QStandardPaths::MoviesLocation);
    if (videos.isEmpty()) videos = QDir::homePath() + QStringLiteral("/Videos");
    return QDir(videos).filePath(QStringLiteral("EditPathSessions"));
}

bool prepareRenderSafetyConfig(const QString &configName, const QString &session, QString *problem)
{
    const QString configRoot = QStandardPaths::writableLocation(QStandardPaths::GenericConfigLocation);
    const QString renderTemp = QDir(session).filePath(QStringLiteral("render-temp"));
    if (configRoot.isEmpty() || !QDir().mkpath(configRoot) || !QDir().mkpath(renderTemp)) {
        if (problem) *problem = QStringLiteral("Could not prepare the memory-safe render folders.");
        return false;
    }
    QSettings config(QDir(configRoot).filePath(configName), QSettings::IniFormat);
    config.beginGroup(QStringLiteral("project"));
    config.setValue(QStringLiteral("parallelrender"), false);
    config.endGroup();
    config.beginGroup(QStringLiteral("tools"));
    config.setValue(QStringLiteral("processingthreads"), 1);
    config.setValue(QStringLiteral("encodethreads"), 2);
    config.setValue(QStringLiteral("currenttmpfolder"), QDir::toNativeSeparators(renderTemp));
    config.endGroup();
    config.sync();
    if (config.status() != QSettings::NoError) {
        if (problem) *problem = QStringLiteral("Could not save the memory-safe render settings.");
        return false;
    }
    return true;
}

QString qtMultimediaQmlPath()
{
    QStringList roots;
    // Craft's Windows portable layout places the QML import tree beside the
    // executables (for example bin/QtMultimedia/qmldir), not only in ../qml.
    roots.append(QCoreApplication::applicationDirPath());
    roots.append(qEnvironmentVariable("QML2_IMPORT_PATH").split(QDir::listSeparator(), Qt::SkipEmptyParts));
    roots.append(QLibraryInfo::path(QLibraryInfo::QmlImportsPath));
    roots.append(QDir(QCoreApplication::applicationDirPath()).filePath(QStringLiteral("../qml")));
    for (const QString &root : std::as_const(roots)) {
        const QString qmldir = QDir(root).filePath(QStringLiteral("QtMultimedia/qmldir"));
        if (QFileInfo(qmldir).isReadable()) return qmldir;
    }
    return {};
}

QString guiRuntimeProblem()
{
    if (qtMultimediaQmlPath().isEmpty()) {
        return QStringLiteral(
            "QtMultimedia QML is missing. On openSUSE install qt6-multimedia-imports; Kdenlive cannot safely create its timeline without this module.");
    }
    return {};
}

QString friendlyFinalizationError(const QString &details)
{
    const QString value = details.toLower();
    if (value.contains(QStringLiteral("rendered video")) && value.contains(QStringLiteral("exactly one"))) {
        if (value.contains(QStringLiteral("found 0"))) {
            return QStringLiteral("We couldn't find your final rendered video. Render the finished timeline into the session folder, then try again.");
        }
        return QStringLiteral("We found more than one possible final video. Keep only your finished render in the session folder, then try again.");
    }
    if (value.contains(QStringLiteral("contains no resolvable media")) || value.contains(QStringLiteral("no resolvable media resources"))) {
        return QStringLiteral("The saved project does not contain usable media yet. Return to Kdenlive, add your clips to the project, save, and try again.");
    }
    if (value.contains(QStringLiteral("checkpoint")) || value.contains(QStringLiteral("hash_chain")) || value.contains(QStringLiteral("branch_resolution"))) {
        return QStringLiteral("We could not verify every recorded editing step. Your project and render are safe, but this session needs technical review "
                              "before it can become a dataset sample.");
    }
    if (value.contains(QStringLiteral("final render validation")) || value.contains(QStringLiteral("media reconstruction"))) {
        return QStringLiteral(
            "The recreated video did not match your final render closely enough. Check that you rendered the latest saved timeline, then try again.");
    }
    if (value.contains(QStringLiteral("melt")) || value.contains(QStringLiteral("ffmpeg")) || value.contains(QStringLiteral("ffprobe"))) {
        return QStringLiteral("A required video-processing component is unavailable. Your edit is safe; ask the EditPath team to check this installation.");
    }
    if (value.contains(QStringLiteral("completed sample already exists"))) {
        return QStringLiteral("A dataset sample has already been created for this session. Open it below or start a new edit.");
    }
    return QStringLiteral(
        "We couldn't create the dataset sample. Your project and final video are safe. Open technical details and share them with the EditPath team.");
}
} // namespace

class RecorderWindow final : public QMainWindow
{
public:
    RecorderWindow()
        : m_repoRoot(repositoryRoot())
    {
        setWindowTitle(QStringLiteral("EditPath"));
        resize(760, 540);
        buildUi();
        restoreLastSession();
        const QString runtimeProblem = guiRuntimeProblem();
        if (m_repoRoot.isEmpty()) {
            setStatus(QStringLiteral("EditPath is not installed correctly. Your files are unaffected; please contact the EditPath team."), true);
            show();
        } else if (!runtimeProblem.isEmpty()) {
            setStatus(QStringLiteral("EditPath is missing a required video component. Your files are unaffected; please contact the EditPath team."), true);
            m_activity->appendPlainText(runtimeProblem);
            show();
        } else {
            QTimer::singleShot(0, this, [this] {
                if (m_showExistingCompletion) {
                    showCompletionWindow();
                } else {
                    startNewSession();
                }
            });
        }
    }

protected:
    void closeEvent(QCloseEvent *event) override
    {
        if (m_audioCapture.state() != QProcess::NotRunning) {
            m_audioCapture.terminate();
            if (!m_audioCapture.waitForFinished(3000)) m_audioCapture.kill();
        }
        if (m_editor.state() != QProcess::NotRunning || m_worker.state() != QProcess::NotRunning) {
            QMessageBox::warning(this, QStringLiteral("Please wait"),
                                 QStringLiteral("EditPath is still working. Wait for it to finish, or close Kdenlive normally first."));
            event->ignore();
            return;
        }
        event->accept();
    }

private:
    void buildUi()
    {
        auto *central = new QWidget;
        auto *layout = new QVBoxLayout(central);
        layout->setContentsMargins(28, 24, 28, 24);
        layout->setSpacing(12);
        m_title = new QLabel(QStringLiteral("<h1>EditPath</h1><p>Your editing session is safe. Follow the next step shown below.</p>"));
        m_title->setWordWrap(true);
        layout->addWidget(m_title);
        m_instructions = new QLabel(QStringLiteral(
            "<b>When your edit is finished:</b> save and render normally in Kdenlive, then close the editor and click <b>Create Dataset Sample</b>. "
            "EditPath presets the output inside this session and "
            "also discovers a different destination selected in Kdenlive automatically. No file copying or dragging is required. The project is saved "
            "automatically as <b>edit.kdenlive</b>."));
        m_instructions->setWordWrap(true);
        layout->addWidget(m_instructions);
        m_status = new QLabel;
        m_status->setWordWrap(true);
        layout->addWidget(m_status);
        m_launchProgress = new QProgressBar;
        m_launchProgress->setRange(0, 0);
        m_launchProgress->setTextVisible(false);
        m_launchProgress->setVisible(false);
        layout->addWidget(m_launchProgress);
        setStatus(QStringLiteral("Checking the editing session…"));
        layout->addWidget(new QLabel(QStringLiteral("<b>Where your work is saved</b>")));
        m_sessionLabel = new QLabel(QStringLiteral("No session created"));
        m_sessionLabel->setWordWrap(true);
        m_sessionLabel->setTextInteractionFlags(Qt::TextSelectableByMouse);
        layout->addWidget(m_sessionLabel);

        auto *primary = new QHBoxLayout;
        m_start = new QPushButton(QStringLiteral("Start New Edit"));
        m_start->setMinimumHeight(42);
        m_start->setVisible(false);
        m_recover = new QPushButton(QStringLiteral("Resume Editing"));
        m_recover->setMinimumHeight(42);
        m_recover->setVisible(false);
        m_finish = new QPushButton(QStringLiteral("Create Dataset Sample"));
        m_finish->setToolTip(QStringLiteral("Create Edit Replay after the final render"));
        m_finish->setMinimumHeight(42);
        m_finish->setEnabled(false);
        primary->addWidget(m_start);
        primary->addWidget(m_recover);
        primary->addWidget(m_finish);
        m_recordReasoning = new QPushButton(QStringLiteral("Record Reasoning"));
        m_stopReasoning = new QPushButton(QStringLiteral("Stop Reasoning"));
        m_recordReasoning->setMinimumHeight(42);
        m_stopReasoning->setMinimumHeight(42);
        m_stopReasoning->setEnabled(false);
        primary->addWidget(m_recordReasoning);
        primary->addWidget(m_stopReasoning);
        layout->addLayout(primary);
        auto *secondary = new QHBoxLayout;
        m_openSession = new QPushButton(QStringLiteral("Open Session Folder"));
        m_openSession->setEnabled(false);
        m_openCompleted = new QPushButton(QStringLiteral("Open Dataset Sample"));
        m_openCompleted->setEnabled(false);
        secondary->addWidget(m_openSession);
        secondary->addWidget(m_openCompleted);
        secondary->addStretch();
        layout->addLayout(secondary);
        m_toggleDetails = new QPushButton(QStringLiteral("Show technical details"));
        m_toggleDetails->setFlat(true);
        layout->addWidget(m_toggleDetails, 0, Qt::AlignLeft);
        m_activity = new QPlainTextEdit;
        m_activity->setReadOnly(true);
        m_activity->setMaximumBlockCount(300);
        m_activity->setVisible(false);
        layout->addWidget(m_activity, 1);
        setCentralWidget(central);
        connect(m_toggleDetails, &QPushButton::clicked, this, [this] {
            const bool show = !m_activity->isVisible();
            m_activity->setVisible(show);
            m_toggleDetails->setText(show ? QStringLiteral("Hide technical details") : QStringLiteral("Show technical details"));
        });

        connect(m_start, &QPushButton::clicked, this, [this] {
            if (m_confirmNewSession && QMessageBox::question(this, QStringLiteral("Start a new edit?"),
                                                             QStringLiteral("This session has not been turned into a dataset sample. Start a new edit anyway?"),
                                                             QMessageBox::Yes | QMessageBox::No, QMessageBox::No) != QMessageBox::Yes) {
                return;
            }
            startNewSession();
        });
        connect(m_recover, &QPushButton::clicked, this, [this] {
            ++m_segment;
            launchSegment();
        });
        connect(m_finish, &QPushButton::clicked, this, &RecorderWindow::finishSession);
        connect(m_recordReasoning, &QPushButton::clicked, this, [this] { startReasoning(); });
        connect(m_stopReasoning, &QPushButton::clicked, this, [this] { stopReasoning(); });
        connect(m_openSession, &QPushButton::clicked, this, [this] { openFolder(m_session, QStringLiteral("Session folder")); });
        connect(m_openCompleted, &QPushButton::clicked, this,
                [this] { openFolder(m_session + QStringLiteral("/completed-sample"), QStringLiteral("Generated sample")); });
        connect(&m_editor, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this, &RecorderWindow::editorFinished);
        connect(&m_editor, &QProcess::started, this, [this] {
            writeManifest(QStringLiteral("recording"));
            m_heartbeat.start();
            m_readyPoll.start();
            setStatus(QStringLiteral("Kdenlive is loading its editor interface. This can take up to 60 seconds over a remote connection."));
            m_activity->appendPlainText(QStringLiteral("Kdenlive process started; waiting for its GUI-ready signal…"));
        });
        connect(&m_editor, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
            if (error == QProcess::FailedToStart) {
                m_heartbeat.stop();
                m_readyPoll.stop();
                m_launchProgress->setVisible(false);
                writeManifest(QStringLiteral("start_failed"));
                setStatus(QStringLiteral("Kdenlive could not open. Your session is safe. Show technical details and share them with the EditPath team."), true);
                m_start->setVisible(true);
                showCompletionWindow();
            }
        });
        connect(&m_worker, &QProcess::readyReadStandardOutput, this, &RecorderWindow::readWorker);
        connect(&m_worker, &QProcess::readyReadStandardError, this, &RecorderWindow::readWorker);
        connect(&m_worker, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this, &RecorderWindow::workerFinished);
        m_heartbeat.setInterval(60000);
        connect(&m_heartbeat, &QTimer::timeout, this, [this] {
            if (m_editor.state() != QProcess::NotRunning) writeManifest(QStringLiteral("recording"));
        });
        m_readyPoll.setInterval(250);
        connect(&m_readyPoll, &QTimer::timeout, this, [this] {
            if (!m_readyFile.isEmpty() && QFileInfo::exists(m_readyFile)) {
                m_readyPoll.stop();
                m_launchProgress->setVisible(false);
                setStatus(QStringLiteral("Kdenlive is ready. You can continue editing."));
                m_activity->appendPlainText(QStringLiteral("Kdenlive reported that its editor interface is ready."));
                QFile acknowledgement(m_readyFile + QStringLiteral(".ack"));
                if (acknowledgement.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
                    acknowledgement.write(QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs).toUtf8());
                }
                if (m_editor.state() != QProcess::NotRunning) hide();
            }
        });
    }

    void setStatus(const QString &text, bool error = false)
    {
        m_status->setText(text);
        m_status->setStyleSheet(error ? QStringLiteral("padding:10px;background:#f7dddd;color:#7d1010;border-radius:4px;")
                                      : QStringLiteral("padding:10px;background:#e2f2e5;color:#164d24;border-radius:4px;"));
    }

    void startReasoning()
    {
        if (m_session.isEmpty() || m_audioCapture.state() != QProcess::NotRunning) return;
        const QString output = QDir(m_session).filePath(QStringLiteral("EDIT-PATH/reasoning/audio-%1.flac").arg(++m_audioIndex, 3, 10, QLatin1Char('0')));
        QDir().mkpath(QFileInfo(output).absolutePath());
        QString ffmpeg = QDir(QCoreApplication::applicationDirPath()).filePath(QStringLiteral("ffmpeg.exe"));
        if (!QFileInfo::exists(ffmpeg)) ffmpeg = QStandardPaths::findExecutable(QStringLiteral("ffmpeg"));
        if (ffmpeg.isEmpty()) { setStatus(QStringLiteral("FFmpeg was not found; reasoning audio was not started."), true); return; }
        m_audioOutput = output;
        m_audioCapture.start(ffmpeg, {QStringLiteral("-hide_banner"), QStringLiteral("-loglevel"), QStringLiteral("error"), QStringLiteral("-f"), QStringLiteral("dshow"), QStringLiteral("-i"), QStringLiteral("audio=default"), QStringLiteral("-c:a"), QStringLiteral("flac"), QStringLiteral("-y"), output});
        if (m_audioCapture.waitForStarted(3000)) { m_recordReasoning->setEnabled(false); m_stopReasoning->setEnabled(true); setStatus(QStringLiteral("Reasoning audio recording is active.")); }
        else setStatus(QStringLiteral("Could not start microphone capture. Show technical details."), true);
    }

    void stopReasoning()
    {
        if (m_audioCapture.state() == QProcess::NotRunning) return;
        m_audioCapture.terminate();
        if (!m_audioCapture.waitForFinished(5000)) { m_audioCapture.kill(); m_audioCapture.waitForFinished(2000); }
        m_recordReasoning->setEnabled(true); m_stopReasoning->setEnabled(false);
        m_activity->appendPlainText(QStringLiteral("Reasoning audio saved: %1").arg(m_audioOutput));
        setStatus(QStringLiteral("Reasoning audio saved. Whisper transcription can now be run for this segment."));
    }

    void writeManifest(const QString &status)
    {
        if (m_session.isEmpty()) return;
        QJsonObject manifest{{QStringLiteral("schema_version"), QStringLiteral("0.3.0")},
                             {QStringLiteral("session_dir"), m_session},
                             {QStringLiteral("session_id"), m_sessionId},
                             {QStringLiteral("config_name"), m_configName},
                             {QStringLiteral("render_output"), QDir(m_session).filePath(QStringLiteral("editor-final.mp4"))},
                             {QStringLiteral("segment"), m_segment},
                             {QStringLiteral("status"), status},
                             {QStringLiteral("kdenlive_pid"), qint64(m_editor.processId())},
                             {QStringLiteral("last_exit_code"), m_lastEditorExitCode},
                             {QStringLiteral("last_exit_crashed"), m_lastEditorExitCrashed},
                             {QStringLiteral("updated_at_utc"), QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)}};
        QSaveFile file(QDir(m_session).filePath(QStringLiteral("session.json")));
        const QByteArray encoded = QJsonDocument(manifest).toJson(QJsonDocument::Indented);
        if (file.open(QIODevice::WriteOnly) && file.write(encoded) == encoded.size()) file.commit();
        QSettings().setValue(QStringLiteral("lastSession"), m_session);
    }

    void restoreLastSession()
    {
        const QString previous = QSettings().value(QStringLiteral("lastSession")).toString();
        QFile file(QDir(previous).filePath(QStringLiteral("session.json")));
        if (previous.isEmpty() || !file.open(QIODevice::ReadOnly)) return;
        const auto manifest = QJsonDocument::fromJson(file.readAll()).object();
        const QString schemaVersion = manifest.value(QStringLiteral("schema_version")).toString();
        if (schemaVersion != QStringLiteral("0.2.0") && schemaVersion != QStringLiteral("0.3.0")) return;
        m_session = previous;
        m_sessionId = manifest.value(QStringLiteral("session_id")).toString();
        if (m_sessionId.isEmpty()) m_sessionId = QFileInfo(previous).fileName();
        m_configName = manifest.value(QStringLiteral("config_name")).toString();
        m_segment = manifest.value(QStringLiteral("segment")).toInt();
        m_sessionLabel->setText(m_session);
        m_openSession->setEnabled(true);
        const QString status = manifest.value(QStringLiteral("status")).toString();
        if (status == QStringLiteral("ready_to_finish")) {
            m_finish->setEnabled(true);
            offerConfirmedNewSession();
            setStatus(QStringLiteral("Your previous edit is ready. Add the final rendered video to this folder, then create the dataset sample."));
            m_showExistingCompletion = true;
        } else if (status == QStringLiteral("recovery_available") || status == QStringLiteral("recording")) {
            const QString project = QDir(previous).filePath(QStringLiteral("edit.kdenlive"));
            const bool canRecover = QFileInfo::exists(project);
            m_recover->setVisible(canRecover);
            offerConfirmedNewSession();
            writeManifest(QStringLiteral("recovery_available"));
            setStatus(canRecover ? QStringLiteral(
                                       "Kdenlive closed unexpectedly, but your saved work is available. Click Resume Editing to continue where you left off.")
                                 : QStringLiteral("Kdenlive closed before the project could be saved. The session folder is available for technical review."),
                      true);
            m_activity->appendPlainText(canRecover
                                            ? QStringLiteral("Interrupted session detected. Recovery will create recording segment %1.").arg(m_segment + 1)
                                            : QStringLiteral("Interrupted session detected, but edit.kdenlive is missing."));
            m_showExistingCompletion = true;
        } else if (status == QStringLiteral("packaged")) {
            m_openCompleted->setEnabled(true);
            setStatus(QStringLiteral("Your dataset sample is ready."));
            m_start->setVisible(true);
            m_showExistingCompletion = true;
        }
    }

    void startNewSession()
    {
        m_confirmNewSession = false;
        m_start->setText(QStringLiteral("Start New Edit"));
        const QString stamp = QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd_HHmmss"));
        const QString suffix = QUuid::createUuid().toString(QUuid::WithoutBraces).left(8);
        m_session = QDir(sessionsRoot()).filePath(QStringLiteral("session_%1_%2").arg(stamp, suffix));
        m_sessionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
        m_configName = QStringLiteral("edit-path-%1rc").arg(suffix);
        m_segment = 1;
        if (!QDir().mkpath(m_session)) {
            setStatus(QStringLiteral("EditPath could not create a folder for this edit. Check that your Videos folder is writable, then try again."), true);
            return;
        }
        m_sessionLabel->setText(m_session);
        m_openSession->setEnabled(true);
        m_openCompleted->setEnabled(false);
        writeManifest(QStringLiteral("created"));
        launchSegment();
    }

    void launchSegment()
    {
        const QString runtimeProblem = guiRuntimeProblem();
        if (!runtimeProblem.isEmpty()) {
            setStatus(runtimeProblem, true);
            m_activity->appendPlainText(runtimeProblem);
            showCompletionWindow();
            return;
        }
        showCompletionWindow();
        m_instructions->setVisible(false);
        m_title->setText(QStringLiteral("<h1>Opening your editor…</h1><p>EditPath is preparing Kdenlive and protecting your session.</p>"));
        m_launchProgress->setVisible(true);
        m_recover->setVisible(false);
        m_start->setVisible(false);
        m_finish->setEnabled(false);
        const QString number = QStringLiteral("%1").arg(m_segment, 3, 10, QLatin1Char('0'));
        const QString journal = QDir(m_session).filePath(QStringLiteral("EDIT-PATH"));
        QDir().mkpath(journal);
        const QString raw = QDir(journal).filePath(QStringLiteral("events-%1.jsonl").arg(number));
        const QString console = QDir(m_session).filePath(QStringLiteral("kdenlive-console-%1.log").arg(number));
        const QString project = QDir(m_session).filePath(QStringLiteral("edit.kdenlive"));
        const QString renderOutput = QDir(m_session).filePath(QStringLiteral("editor-final.mp4"));
        const QString renderTemp = QDir(m_session).filePath(QStringLiteral("render-temp"));
        m_readyFile = QDir(m_session).filePath(QStringLiteral("kdenlive-ready-%1").arg(number));
        QFile::remove(m_readyFile);
        QString renderSafetyProblem;
        if (!prepareRenderSafetyConfig(m_configName, m_session, &renderSafetyProblem)) {
            m_activity->appendPlainText(renderSafetyProblem);
        } else {
            m_activity->appendPlainText(QStringLiteral("Memory-safe rendering enabled: 1 processing thread, 2 encoder threads, session-local temp storage."));
        }
        QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_CONFIG"), m_configName);
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_PROJECT"), project);
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_RENDER_OUTPUT"), renderOutput);
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_LOG"), raw);
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_SESSION_ID"), m_sessionId);
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_SEGMENT"), QString::number(m_segment));
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_STATE_DIR"), QDir(m_session).filePath(QStringLiteral("states")));
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_ENTITY_MAP"), QDir(m_session).filePath(QStringLiteral("entity-map.json")));
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_READY_FILE"), m_readyFile);
        environment.insert(QStringLiteral("TEMP"), QDir::toNativeSeparators(renderTemp));
        environment.insert(QStringLiteral("TMP"), QDir::toNativeSeparators(renderTemp));
        environment.insert(QStringLiteral("OMP_NUM_THREADS"), QStringLiteral("2"));
        environment.remove(QStringLiteral("KDENLIVE_VIDEO_PATH_CLIPS"));
        environment.remove(QStringLiteral("QSG_RHI_BACKEND"));
        environment.remove(QStringLiteral("LIBGL_ALWAYS_SOFTWARE"));
        m_editor.setProcessEnvironment(environment);
        m_editor.setWorkingDirectory(m_repoRoot);
        m_editor.setProcessChannelMode(QProcess::MergedChannels);
        m_editor.setStandardOutputFile(console, QIODevice::Append);
        setStatus(QStringLiteral("Please wait. This window will hide automatically when Kdenlive is ready."));
        m_activity->appendPlainText(QStringLiteral("Starting recording segment %1…").arg(number));
        QString program;
        QStringList arguments;
#ifdef Q_OS_WIN
        program = QDir(QCoreApplication::applicationDirPath()).filePath(QStringLiteral("kdenlive.exe"));
        arguments = {QStringLiteral("--config"), m_configName, QStringLiteral("--no-welcome")};
        if (QFileInfo::exists(project)) arguments.append(project);
#else
        program = m_repoRoot + QStringLiteral("/video-path-pilot/run-video-path-pilot.sh");
        arguments = {raw};
        if (QFileInfo::exists(project)) arguments.append(project);
#endif
        m_editor.start(program, arguments);
    }

    void editorFinished(int exitCode, QProcess::ExitStatus exitStatus)
    {
        m_heartbeat.stop();
        m_readyPoll.stop();
        m_launchProgress->setVisible(false);
        m_lastEditorExitCode = exitCode;
        m_lastEditorExitCrashed = exitStatus != QProcess::NormalExit || exitCode != 0;
        if (m_lastEditorExitCrashed) writeManifest(QStringLiteral("recovery_available"));
        m_title->setText(QStringLiteral("<h1>EditPath</h1><p>Kdenlive has closed. Your work is being checked and saved.</p>"));
        m_instructions->setVisible(true);
        showCompletionWindow();
        m_activity->appendPlainText(m_lastEditorExitCrashed
                                        ? QStringLiteral("Kdenlive crashed with signal/exit code %1; checking recoverable evidence…").arg(exitCode)
                                        : QStringLiteral("Kdenlive exited with code %1; checking the recording…").arg(exitCode));
        m_workerPurpose = QStringLiteral("validate");
        const QString raw = QDir(m_session).filePath(QStringLiteral("EDIT-PATH/events-%1.jsonl").arg(m_segment, 3, 10, QLatin1Char('0')));
        m_worker.start(pythonExecutable(), {m_repoRoot + QStringLiteral("/video-path-pilot/validate_video_path.py"), raw});
    }

    void finishSession()
    {
        m_finish->setEnabled(false);
        m_workerPurpose = QStringLiteral("finalize");
        m_workerTranscript.clear();
        m_instructions->setVisible(false);
        m_title->setText(
            QStringLiteral("<h1>Creating your dataset sample…</h1><p>You can leave the files where they are. EditPath is doing the packaging and checks.</p>"));
        setStatus(QStringLiteral("This can take several minutes for a long edit. Keep EditPath open until it finishes."));
        m_worker.start(pythonExecutable(), {m_repoRoot + QStringLiteral("/video-path-pilot/job_pipeline.py"), QStringLiteral("finalize-freeform"), m_session});
    }

    void readWorker()
    {
        const QString output = QString::fromUtf8(m_worker.readAllStandardOutput()).trimmed();
        const QString errors = QString::fromUtf8(m_worker.readAllStandardError()).trimmed();
        if (!output.isEmpty()) m_activity->appendPlainText(output);
        if (!errors.isEmpty()) m_activity->appendPlainText(errors);
        if (!output.isEmpty() || !errors.isEmpty()) {
            const QString message = output + (output.isEmpty() || errors.isEmpty() ? QString() : QStringLiteral("\n")) + errors;
            m_workerTranscript += message + QStringLiteral("\n");
            if (!m_session.isEmpty()) {
                QFile log(QDir(m_session).filePath(QStringLiteral("supervisor-activity.log")));
                if (log.open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text)) log.write((message + QStringLiteral("\n")).toUtf8());
            }
        }
    }

    void workerFinished(int exitCode, QProcess::ExitStatus status)
    {
        readWorker();
        const bool success = status == QProcess::NormalExit && exitCode == 0;
        if (m_workerPurpose == QStringLiteral("validate")) {
            if (success && !m_lastEditorExitCrashed && m_lastEditorExitCode == 0) {
                writeManifest(QStringLiteral("ready_to_finish"));
                m_finish->setEnabled(true);
                offerConfirmedNewSession();
                setStatus(QStringLiteral(
                    "Your editing steps were saved. Click Create Edit Replay after rendering; EditPath combines every EDIT-PATH journal segment, locates the Kdenlive render, and generates the replay."));
            } else if (m_lastEditorExitCrashed) {
                writeManifest(QStringLiteral("recovery_available"));
                m_recover->setVisible(true);
                offerConfirmedNewSession();
                setStatus(QStringLiteral(
                              "Kdenlive closed unexpectedly, but your saved project and recorded editing steps are safe. Click Resume Editing to continue."),
                          true);
            } else {
                writeManifest(QStringLiteral("validation_failed"));
                offerConfirmedNewSession();
                setStatus(
                    QStringLiteral(
                        "We could not verify the recorded editing steps. Your project is safe. Show technical details and share them with the EditPath team."),
                    true);
            }
        } else if (m_workerPurpose == QStringLiteral("finalize")) {
            if (success) {
                writeManifest(QStringLiteral("packaged"));
                m_openCompleted->setEnabled(true);
                m_confirmNewSession = false;
                m_start->setText(QStringLiteral("Start New Edit"));
                m_start->setVisible(true);
                QFile reportFile(m_session + QStringLiteral("/completed-sample/verification/report.json"));
                bool mediaPassed = false;
                if (reportFile.open(QIODevice::ReadOnly))
                    mediaPassed = QJsonDocument::fromJson(reportFile.readAll())
                                      .object()
                                      .value(QStringLiteral("final"))
                                      .toObject()
                                      .value(QStringLiteral("accepted"))
                                      .toBool();
                m_title->setText(
                    QStringLiteral("<h1>Your dataset sample is ready</h1><p>EditPath saved the complete item and verified the recreated video.</p>"));
                setStatus(mediaPassed ? QStringLiteral("Done. You can open the dataset sample below or start a new edit.")
                                      : QStringLiteral("The sample was created, but the recreated video needs technical review. Your original edit is safe."),
                          !mediaPassed);
            } else {
                m_finish->setEnabled(true);
                m_instructions->setVisible(true);
                m_title->setText(
                    QStringLiteral("<h1>We couldn't create the sample yet</h1><p>Your project and rendered video have not been deleted or changed.</p>"));
                setStatus(friendlyFinalizationError(m_workerTranscript), true);
            }
        }
        m_workerPurpose.clear();
    }

    void showCompletionWindow()
    {
        show();
        raise();
        activateWindow();
    }

    void offerConfirmedNewSession()
    {
        m_confirmNewSession = true;
        m_start->setText(QStringLiteral("Start New Edit Anyway"));
        m_start->setVisible(true);
    }

    void openFolder(const QString &path, const QString &label)
    {
        const QString nativePath = QDir::toNativeSeparators(path);
        QApplication::clipboard()->setText(nativePath);
        bool launched = false;
#ifdef Q_OS_LINUX
        // Desktop URL dispatch commonly has no usable portal/DBus service in
        // forwarded-X11 sessions. Prefer an installed file manager there.
        if (!qEnvironmentVariableIsEmpty("SSH_CONNECTION") && !qEnvironmentVariableIsEmpty("DISPLAY")) {
            const QString fileManager = QStandardPaths::findExecutable(QStringLiteral("nautilus"));
            if (!fileManager.isEmpty()) {
                launched = QProcess::startDetached(fileManager, {QStringLiteral("--no-desktop"), path});
            }
        }
#endif
        if (!launched) {
            launched = QDesktopServices::openUrl(QUrl::fromLocalFile(path));
        }
        m_activity->appendPlainText(launched ? QStringLiteral("%1 open requested; path copied to clipboard: %2").arg(label, nativePath)
                                             : QStringLiteral("Could not open %1; path copied to clipboard: %2").arg(label, nativePath));
        if (!launched) {
            QMessageBox::information(
                this, QStringLiteral("Folder path copied"),
                QStringLiteral("No desktop file manager is available. The %1 path was copied to the clipboard:\n%2").arg(label.toLower(), nativePath));
        }
    }

    QString m_repoRoot, m_session, m_sessionId, m_configName, m_workerPurpose, m_readyFile, m_workerTranscript;
    int m_segment{0};
    int m_lastEditorExitCode{0};
    QProcess m_editor, m_worker, m_audioCapture;
    QTimer m_heartbeat, m_readyPoll;
    bool m_showExistingCompletion{false}, m_lastEditorExitCrashed{false}, m_confirmNewSession{false};
    QLabel *m_title{}, *m_instructions{}, *m_status{}, *m_sessionLabel{};
    QPushButton *m_start{}, *m_recover{}, *m_finish{}, *m_recordReasoning{}, *m_stopReasoning{}, *m_openSession{}, *m_openCompleted{}, *m_toggleDetails{};
    QPlainTextEdit *m_activity{};
    QProgressBar *m_launchProgress{};
    QString m_audioOutput;
    int m_audioIndex{0};
};

int runSelfTest()
{
    const QString appDirectory = QCoreApplication::applicationDirPath();
    const QString root = repositoryRoot();
    QJsonObject checks;
    auto checkFile = [&checks](const QString &name, const QString &path) {
        const bool present = QFileInfo::exists(path);
        checks.insert(name, QJsonObject{{QStringLiteral("passed"), present}, {QStringLiteral("path"), QDir::toNativeSeparators(path)}});
        return present;
    };

#ifdef Q_OS_WIN
    const QString kdenlive = QDir(appDirectory).filePath(QStringLiteral("kdenlive.exe"));
    const QString ffmpeg = QDir(appDirectory).filePath(QStringLiteral("ffmpeg.exe"));
    const QString ffprobe = QDir(appDirectory).filePath(QStringLiteral("ffprobe.exe"));
    const QString melt = QDir(appDirectory).filePath(QStringLiteral("melt.exe"));
#else
    const QString kdenlive = QDir(appDirectory).filePath(QStringLiteral("kdenlive"));
    const QString ffmpeg = QStandardPaths::findExecutable(QStringLiteral("ffmpeg"));
    const QString ffprobe = QStandardPaths::findExecutable(QStringLiteral("ffprobe"));
    QString melt = QStandardPaths::findExecutable(QStringLiteral("melt"));
    if (melt.isEmpty()) melt = QStandardPaths::findExecutable(QStringLiteral("mlt-melt"));
#endif
    bool passed = !root.isEmpty();
    checks.insert(QStringLiteral("application_root"),
                  QJsonObject{{QStringLiteral("passed"), !root.isEmpty()}, {QStringLiteral("path"), QDir::toNativeSeparators(root)}});
    QTemporaryDir renderSafetyRoot;
    const QString renderSafetyName = QStringLiteral("edit-path-self-test-%1rc").arg(QUuid::createUuid().toString(QUuid::WithoutBraces));
    QString renderSafetyProblem;
    const bool renderSafetyPassed = [&]() {
        if (!renderSafetyRoot.isValid() || !prepareRenderSafetyConfig(renderSafetyName, renderSafetyRoot.path(), &renderSafetyProblem)) return false;
        QSettings config(QDir(QStandardPaths::writableLocation(QStandardPaths::GenericConfigLocation)).filePath(renderSafetyName), QSettings::IniFormat);
        return !config.value(QStringLiteral("project/parallelrender"), true).toBool()
            && config.value(QStringLiteral("tools/processingthreads"), 0).toInt() == 1
            && config.value(QStringLiteral("tools/encodethreads"), 0).toInt() == 2
            && !config.value(QStringLiteral("tools/currenttmpfolder")).toString().isEmpty();
    }();
    QFile::remove(QDir(QStandardPaths::writableLocation(QStandardPaths::GenericConfigLocation)).filePath(renderSafetyName));
    checks.insert(QStringLiteral("memory_safe_rendering"),
                  QJsonObject{{QStringLiteral("passed"), renderSafetyPassed}, {QStringLiteral("error"), renderSafetyProblem}});
    passed = renderSafetyPassed && passed;
    passed = checkFile(QStringLiteral("kdenlive"), kdenlive) && passed;
    passed = checkFile(QStringLiteral("ffmpeg"), ffmpeg) && passed;
    passed = checkFile(QStringLiteral("ffprobe"), ffprobe) && passed;
    passed = checkFile(QStringLiteral("melt"), melt) && passed;
    passed = checkFile(QStringLiteral("qt_multimedia_qml"), qtMultimediaQmlPath()) && passed;
    const QString validator = QDir(root).filePath(QStringLiteral("video-path-pilot/validate_video_path.py"));
    passed = checkFile(QStringLiteral("validator"), validator) && passed;
    const QString pipeline = QDir(root).filePath(QStringLiteral("video-path-pilot/job_pipeline.py"));
    passed = checkFile(QStringLiteral("pipeline"), pipeline) && passed;
    const QString editPathModule = QDir(root).filePath(QStringLiteral("edit_path/__main__.py"));
    passed = checkFile(QStringLiteral("edit_path_module"), editPathModule) && passed;
    QString python = pythonExecutable();
    if (!QFileInfo(python).isAbsolute()) python = QStandardPaths::findExecutable(python);
    passed = checkFile(QStringLiteral("python"), python) && passed;

    QProcess doctorTest;
    QProcessEnvironment doctorEnvironment = QProcessEnvironment::systemEnvironment();
    const QString existingPythonPath = doctorEnvironment.value(QStringLiteral("PYTHONPATH"));
    doctorEnvironment.insert(QStringLiteral("PYTHONPATH"), existingPythonPath.isEmpty() ? root : root + QDir::listSeparator() + existingPythonPath);
    doctorTest.setProcessEnvironment(doctorEnvironment);
    doctorTest.setWorkingDirectory(root);
    doctorTest.start(python, {QStringLiteral("-m"), QStringLiteral("edit_path"), QStringLiteral("doctor")});
    const bool doctorStarted = doctorTest.waitForStarted(10000);
    const bool doctorFinished = doctorStarted && doctorTest.waitForFinished(30000);
    const bool doctorPassed = doctorFinished && doctorTest.exitStatus() == QProcess::NormalExit && doctorTest.exitCode() == 0;
    QJsonObject pipelineCheck{{QStringLiteral("passed"), doctorPassed}, {QStringLiteral("exit_code"), doctorFinished ? doctorTest.exitCode() : -1}};
    if (!doctorPassed) {
        pipelineCheck.insert(QStringLiteral("error"), doctorTest.errorString());
        pipelineCheck.insert(QStringLiteral("output"), QString::fromUtf8(doctorTest.readAllStandardOutput() + doctorTest.readAllStandardError()));
    }
    checks.insert(QStringLiteral("reconstruction_runtime"), pipelineCheck);
    passed = doctorPassed && passed;

    QProcess packagingTest;
    packagingTest.setProcessEnvironment(doctorEnvironment);
    packagingTest.setWorkingDirectory(root);
    packagingTest.start(python, {pipeline, QStringLiteral("--help")});
    const bool packagingStarted = packagingTest.waitForStarted(10000);
    const bool packagingFinished = packagingStarted && packagingTest.waitForFinished(30000);
    const bool packagingPassed = packagingFinished && packagingTest.exitStatus() == QProcess::NormalExit && packagingTest.exitCode() == 0;
    QJsonObject packagingCheck{{QStringLiteral("passed"), packagingPassed}, {QStringLiteral("exit_code"), packagingFinished ? packagingTest.exitCode() : -1}};
    if (!packagingPassed) {
        packagingCheck.insert(QStringLiteral("error"), packagingTest.errorString());
        packagingCheck.insert(QStringLiteral("output"), QString::fromUtf8(packagingTest.readAllStandardOutput() + packagingTest.readAllStandardError()));
    }
    checks.insert(QStringLiteral("packaging_pipeline"), packagingCheck);
    passed = packagingPassed && passed;

    const QJsonObject report{
        {QStringLiteral("schema_version"), QStringLiteral("0.1.0")}, {QStringLiteral("passed"), passed}, {QStringLiteral("checks"), checks}};
    const QByteArray encoded = QJsonDocument(report).toJson(QJsonDocument::Indented);
    QTextStream(stdout) << QString::fromUtf8(encoded);
    const QString reportPath = qEnvironmentVariable("EDIT_PATH_SELF_TEST_REPORT");
    if (!reportPath.isEmpty()) {
        QFile reportFile(reportPath);
        if (!reportFile.open(QIODevice::WriteOnly | QIODevice::Truncate) || reportFile.write(encoded) != encoded.size()) return EXIT_FAILURE;
    }
    return passed ? EXIT_SUCCESS : EXIT_FAILURE;
}

int main(int argc, char **argv)
{
    QApplication application(argc, argv);
    QCoreApplication::setOrganizationName(QStringLiteral("Parsewave"));
    QCoreApplication::setApplicationName(QStringLiteral("EditPathRecorder"));
    if (application.arguments().contains(QStringLiteral("--self-test"))) return runSelfTest();
    RecorderWindow window;
    return application.exec();
}
