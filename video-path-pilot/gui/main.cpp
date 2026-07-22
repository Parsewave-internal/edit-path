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
#include <QPushButton>
#include <QSaveFile>
#include <QSettings>
#include <QStandardPaths>
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

QString qtMultimediaQmlPath()
{
    QStringList roots = qEnvironmentVariable("QML2_IMPORT_PATH").split(QDir::listSeparator(), Qt::SkipEmptyParts);
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
} // namespace

class RecorderWindow final : public QMainWindow
{
public:
    RecorderWindow()
        : m_repoRoot(repositoryRoot())
    {
        setWindowTitle(QStringLiteral("Edit Path Recorder MVP"));
        resize(720, 500);
        buildUi();
        restoreLastSession();
        const QString runtimeProblem = guiRuntimeProblem();
        if (m_repoRoot.isEmpty()) {
            setStatus(QStringLiteral("Recorder installation was not found."), true);
            show();
        } else if (!runtimeProblem.isEmpty()) {
            setStatus(runtimeProblem, true);
            m_activity->appendPlainText(runtimeProblem);
            show();
        } else {
            QTimer::singleShot(0, this, [this] {
                if (m_showExistingCompletion) {
                    showCompletionWindow();
                } else if (m_autoRecover) {
                    ++m_segment;
                    launchSegment();
                } else {
                    startNewSession();
                }
            });
        }
    }

protected:
    void closeEvent(QCloseEvent *event) override
    {
        if (m_editor.state() != QProcess::NotRunning || m_worker.state() != QProcess::NotRunning) {
            QMessageBox::warning(this, QStringLiteral("Task active"), QStringLiteral("Wait for the active task or close Kdenlive normally."));
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
        m_title = new QLabel(QStringLiteral("<h1>Editing Session</h1><p>Kdenlive has closed. Review the session status below.</p>"));
        m_title->setWordWrap(true);
        layout->addWidget(m_title);
        m_instructions =
            new QLabel(QStringLiteral("Before finishing, render exactly one .mp4, .mov, .mkv, or .webm directly into the displayed session folder. "
                                      "H.264 is not required. The project is saved automatically as <b>edit.kdenlive</b>."));
        m_instructions->setWordWrap(true);
        layout->addWidget(m_instructions);
        m_status = new QLabel;
        m_status->setWordWrap(true);
        layout->addWidget(m_status);
        setStatus(QStringLiteral("Checking the editing session…"));
        layout->addWidget(new QLabel(QStringLiteral("<b>Session folder</b>")));
        m_sessionLabel = new QLabel(QStringLiteral("No session created"));
        m_sessionLabel->setWordWrap(true);
        m_sessionLabel->setTextInteractionFlags(Qt::TextSelectableByMouse);
        layout->addWidget(m_sessionLabel);

        auto *primary = new QHBoxLayout;
        m_start = new QPushButton(QStringLiteral("Start Another Editing Session"));
        m_start->setMinimumHeight(42);
        m_start->setVisible(false);
        m_recover = new QPushButton(QStringLiteral("Recover and Continue"));
        m_recover->setMinimumHeight(42);
        m_recover->setVisible(false);
        m_finish = new QPushButton(QStringLiteral("Finish Session"));
        m_finish->setMinimumHeight(42);
        m_finish->setEnabled(false);
        primary->addWidget(m_start);
        primary->addWidget(m_recover);
        primary->addWidget(m_finish);
        layout->addLayout(primary);
        auto *secondary = new QHBoxLayout;
        m_openSession = new QPushButton(QStringLiteral("Open Session Folder"));
        m_openSession->setEnabled(false);
        m_openCompleted = new QPushButton(QStringLiteral("Open Generated Sample"));
        m_openCompleted->setEnabled(false);
        secondary->addWidget(m_openSession);
        secondary->addWidget(m_openCompleted);
        secondary->addStretch();
        layout->addLayout(secondary);
        m_activity = new QPlainTextEdit;
        m_activity->setReadOnly(true);
        m_activity->setMaximumBlockCount(300);
        layout->addWidget(m_activity, 1);
        setCentralWidget(central);

        connect(m_start, &QPushButton::clicked, this, [this] {
            if (m_confirmNewSession && QMessageBox::question(this, QStringLiteral("Discard current session?"),
                                                             QStringLiteral("The current session has not been finalized. Start a new session anyway?"),
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
        connect(m_openSession, &QPushButton::clicked, this, [this] { openFolder(m_session, QStringLiteral("Session folder")); });
        connect(m_openCompleted, &QPushButton::clicked, this,
                [this] { openFolder(m_session + QStringLiteral("/completed-sample"), QStringLiteral("Generated sample")); });
        connect(&m_editor, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this, &RecorderWindow::editorFinished);
        connect(&m_editor, &QProcess::started, this, [this] {
            writeManifest(QStringLiteral("recording"));
            m_heartbeat.start();
            m_activity->appendPlainText(QStringLiteral("Kdenlive process started. Remote X11 startup can take 15–60 seconds."));
        });
        connect(&m_editor, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
            if (error == QProcess::FailedToStart) {
                m_heartbeat.stop();
                writeManifest(QStringLiteral("start_failed"));
                setStatus(QStringLiteral("Kdenlive could not start. Check X11 and the console log."), true);
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
    }

    void setStatus(const QString &text, bool error = false)
    {
        m_status->setText(text);
        m_status->setStyleSheet(error ? QStringLiteral("padding:10px;background:#f7dddd;color:#7d1010;border-radius:4px;")
                                      : QStringLiteral("padding:10px;background:#e2f2e5;color:#164d24;border-radius:4px;"));
    }

    void writeManifest(const QString &status)
    {
        if (m_session.isEmpty()) return;
        QJsonObject manifest{{QStringLiteral("schema_version"), QStringLiteral("0.3.0")},
                             {QStringLiteral("session_dir"), m_session},
                             {QStringLiteral("session_id"), m_sessionId},
                             {QStringLiteral("config_name"), m_configName},
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
            setStatus(QStringLiteral("Previous recording is ready to finish."));
            m_showExistingCompletion = true;
        } else if (status == QStringLiteral("recovery_available") || status == QStringLiteral("recording")) {
            const QString project = QDir(previous).filePath(QStringLiteral("edit.kdenlive"));
            m_autoRecover = QFileInfo::exists(project);
        } else if (status == QStringLiteral("packaged")) {
            m_openCompleted->setEnabled(true);
            setStatus(QStringLiteral("Previous sample was generated."));
            m_start->setVisible(true);
            m_showExistingCompletion = true;
        }
    }

    void startNewSession()
    {
        m_confirmNewSession = false;
        m_start->setText(QStringLiteral("Start Another Editing Session"));
        const QString stamp = QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd_HHmmss"));
        const QString suffix = QUuid::createUuid().toString(QUuid::WithoutBraces).left(8);
        m_session = QDir(sessionsRoot()).filePath(QStringLiteral("session_%1_%2").arg(stamp, suffix));
        m_sessionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
        m_configName = QStringLiteral("edit-path-%1rc").arg(suffix);
        m_segment = 1;
        if (!QDir().mkpath(m_session)) {
            setStatus(QStringLiteral("Could not create session folder."), true);
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
        hide();
        m_recover->setVisible(false);
        m_start->setVisible(false);
        m_finish->setEnabled(false);
        const QString number = QStringLiteral("%1").arg(m_segment, 3, 10, QLatin1Char('0'));
        const QString raw = QDir(m_session).filePath(QStringLiteral("raw-events-%1.jsonl").arg(number));
        const QString console = QDir(m_session).filePath(QStringLiteral("kdenlive-console-%1.log").arg(number));
        const QString project = QDir(m_session).filePath(QStringLiteral("edit.kdenlive"));
        QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_CONFIG"), m_configName);
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_PROJECT"), project);
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_LOG"), raw);
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_SESSION_ID"), m_sessionId);
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_SEGMENT"), QString::number(m_segment));
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_STATE_DIR"), QDir(m_session).filePath(QStringLiteral("states")));
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_ENTITY_MAP"), QDir(m_session).filePath(QStringLiteral("entity-map.json")));
        environment.remove(QStringLiteral("KDENLIVE_VIDEO_PATH_CLIPS"));
        environment.remove(QStringLiteral("QSG_RHI_BACKEND"));
        environment.remove(QStringLiteral("LIBGL_ALWAYS_SOFTWARE"));
        m_editor.setProcessEnvironment(environment);
        m_editor.setWorkingDirectory(m_repoRoot);
        m_editor.setProcessChannelMode(QProcess::MergedChannels);
        m_editor.setStandardOutputFile(console, QIODevice::Append);
        setStatus(
            QStringLiteral("Kdenlive is starting. Import or create media normally, save the project and render in the session folder, then close normally."));
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
        m_lastEditorExitCode = exitCode;
        m_lastEditorExitCrashed = exitStatus != QProcess::NormalExit || exitCode != 0;
        showCompletionWindow();
        m_activity->appendPlainText(m_lastEditorExitCrashed
                                        ? QStringLiteral("Kdenlive crashed with signal/exit code %1; checking recoverable evidence…").arg(exitCode)
                                        : QStringLiteral("Kdenlive exited with code %1; checking the recording…").arg(exitCode));
        m_workerPurpose = QStringLiteral("validate");
        const QString raw = QDir(m_session).filePath(QStringLiteral("raw-events-%1.jsonl").arg(m_segment, 3, 10, QLatin1Char('0')));
        m_worker.start(pythonExecutable(), {m_repoRoot + QStringLiteral("/video-path-pilot/validate_video_path.py"), raw});
    }

    void finishSession()
    {
        m_finish->setEnabled(false);
        m_workerPurpose = QStringLiteral("finalize");
        setStatus(QStringLiteral("Discovering project assets, generating sample.json, reconstructing the edit, and comparing renders…"));
        m_worker.start(pythonExecutable(), {m_repoRoot + QStringLiteral("/video-path-pilot/job_pipeline.py"), QStringLiteral("finalize-freeform"), m_session});
    }

    void readWorker()
    {
        const QString output = QString::fromUtf8(m_worker.readAllStandardOutput()).trimmed();
        const QString errors = QString::fromUtf8(m_worker.readAllStandardError()).trimmed();
        if (!output.isEmpty()) m_activity->appendPlainText(output);
        if (!errors.isEmpty()) m_activity->appendPlainText(errors);
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
                    "Recording completed. Put exactly one .kdenlive project and one rendered video in the session folder, then click Finish Session."));
            } else if (m_lastEditorExitCrashed) {
                writeManifest(QStringLiteral("recovery_available"));
                m_recover->setVisible(true);
                offerConfirmedNewSession();
                setStatus(QStringLiteral("Kdenlive exited unexpectedly, but flushed evidence was preserved. Use Recover and Continue, or discard the session "
                                         "if no edit was made."),
                          true);
            } else {
                writeManifest(QStringLiteral("validation_failed"));
                offerConfirmedNewSession();
                setStatus(
                    QStringLiteral(
                        "Kdenlive closed normally, but the recording failed validation. Review Activity; start a fresh session after the cause is fixed."),
                    true);
            }
        } else if (m_workerPurpose == QStringLiteral("finalize")) {
            if (success) {
                writeManifest(QStringLiteral("packaged"));
                m_openCompleted->setEnabled(true);
                m_confirmNewSession = false;
                m_start->setText(QStringLiteral("Start Another Editing Session"));
                m_start->setVisible(true);
                QFile reportFile(m_session + QStringLiteral("/completed-sample/render-report.json"));
                bool mediaPassed = false;
                if (reportFile.open(QIODevice::ReadOnly))
                    mediaPassed = QJsonDocument::fromJson(reportFile.readAll())
                                      .object()
                                      .value(QStringLiteral("final"))
                                      .toObject()
                                      .value(QStringLiteral("accepted"))
                                      .toBool();
                setStatus(mediaPassed
                              ? QStringLiteral("Sample and reconstructed media passed. The verbal task prompt is pending internal entry before client review.")
                              : QStringLiteral("Sample generated, but exact-state media reconstruction failed. Review render-report.json."),
                          !mediaPassed);
            } else {
                m_finish->setEnabled(true);
                setStatus(QStringLiteral("Sample generation failed. Review Activity and correct the project, render, or media files."), true);
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
        m_start->setText(QStringLiteral("Discard and Start New Session"));
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

    QString m_repoRoot, m_session, m_sessionId, m_configName, m_workerPurpose;
    int m_segment{0};
    int m_lastEditorExitCode{0};
    QProcess m_editor, m_worker;
    QTimer m_heartbeat;
    bool m_autoRecover{false}, m_showExistingCompletion{false}, m_lastEditorExitCrashed{false}, m_confirmNewSession{false};
    QLabel *m_title{}, *m_instructions{}, *m_status{}, *m_sessionLabel{};
    QPushButton *m_start{}, *m_recover{}, *m_finish{}, *m_openSession{}, *m_openCompleted{};
    QPlainTextEdit *m_activity{};
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
