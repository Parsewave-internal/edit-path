// SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
// SPDX-License-Identifier: GPL-3.0-only

#include <QApplication>
#include <QCloseEvent>
#include <QDateTime>
#include <QDesktopServices>
#include <QDir>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QMainWindow>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QProcess>
#include <QProcessEnvironment>
#include <QPushButton>
#include <QSettings>
#include <QUrl>
#include <QUuid>
#include <QVBoxLayout>

namespace {
QString repositoryRoot()
{
    const QString configured = qEnvironmentVariable("EDIT_PATH_REPO_ROOT");
    if (!configured.isEmpty() && QFileInfo::exists(configured + QStringLiteral("/video-path-pilot/job_pipeline.py")))
        return QDir(configured).absolutePath();
    QDir current(QCoreApplication::applicationDirPath());
    for (int depth = 0; depth < 6; ++depth) {
        if (QFileInfo::exists(current.filePath(QStringLiteral("video-path-pilot/job_pipeline.py")))) return current.absolutePath();
        if (!current.cdUp()) break;
    }
    return {};
}
}

class RecorderWindow final : public QMainWindow
{
public:
    RecorderWindow() : m_repoRoot(repositoryRoot())
    {
        setWindowTitle(QStringLiteral("Edit Path Recorder MVP"));
        resize(760, 580);
        buildUi();
        const QString previous = QSettings().value(QStringLiteral("lastJob")).toString();
        if (!previous.isEmpty() && QFileInfo::exists(previous + QStringLiteral("/job.json"))) loadJob(previous);
        if (m_repoRoot.isEmpty()) {
            setStatus(QStringLiteral("Recorder installation was not found."), true);
            m_openJob->setEnabled(false);
        }
    }

protected:
    void closeEvent(QCloseEvent *event) override
    {
        if (m_editor.state() != QProcess::NotRunning || m_worker.state() != QProcess::NotRunning) {
            QMessageBox::warning(this, QStringLiteral("Task active"), QStringLiteral("Wait for the active task or close Kdenlive normally."));
            event->ignore(); return;
        }
        event->accept();
    }

private:
    void buildUi()
    {
        auto *central = new QWidget;
        auto *layout = new QVBoxLayout(central);
        auto *title = new QLabel(QStringLiteral("<h1>Edit Path Recorder</h1><p>Open the assigned job, edit in Kdenlive, and finish the job.</p>"));
        title->setWordWrap(true); layout->addWidget(title);

        auto *jobRow = new QHBoxLayout;
        m_openJob = new QPushButton(QStringLiteral("Open Assigned Job"));
        m_jobLabel = new QLabel(QStringLiteral("No job opened")); m_jobLabel->setTextInteractionFlags(Qt::TextSelectableByMouse);
        jobRow->addWidget(m_openJob); jobRow->addWidget(m_jobLabel, 1); layout->addLayout(jobRow);

        m_task = new QLabel(QStringLiteral("Task details will appear here."));
        m_task->setWordWrap(true); m_task->setStyleSheet(QStringLiteral("padding: 10px; background: #eef1f5; border-radius: 4px;"));
        layout->addWidget(m_task);

        m_status = new QLabel; m_status->setWordWrap(true); layout->addWidget(m_status);
        setStatus(QStringLiteral("Open an assigned job to begin."));

        layout->addWidget(new QLabel(QStringLiteral("<b>Session folder</b>")));
        m_sessionLabel = new QLabel(QStringLiteral("No session created"));
        m_sessionLabel->setWordWrap(true); m_sessionLabel->setTextInteractionFlags(Qt::TextSelectableByMouse);
        layout->addWidget(m_sessionLabel);

        auto *primary = new QHBoxLayout;
        m_start = new QPushButton(QStringLiteral("Start Editing Session"));
        m_recover = new QPushButton(QStringLiteral("Recover and Continue"));
        m_finish = new QPushButton(QStringLiteral("Finish Job"));
        m_start->setMinimumHeight(42); m_recover->setMinimumHeight(42); m_finish->setMinimumHeight(42);
        m_start->setEnabled(false); m_recover->setVisible(false); m_finish->setEnabled(false);
        primary->addWidget(m_start); primary->addWidget(m_recover); primary->addWidget(m_finish); layout->addLayout(primary);

        auto *secondary = new QHBoxLayout;
        m_openSession = new QPushButton(QStringLiteral("Open Session Folder"));
        m_openCompleted = new QPushButton(QStringLiteral("Open Completed Sample"));
        m_openSession->setEnabled(false); m_openCompleted->setEnabled(false);
        secondary->addWidget(m_openSession); secondary->addWidget(m_openCompleted); secondary->addStretch(); layout->addLayout(secondary);

        m_activity = new QPlainTextEdit; m_activity->setReadOnly(true); m_activity->setMaximumBlockCount(300);
        layout->addWidget(m_activity, 1); setCentralWidget(central);

        connect(m_openJob, &QPushButton::clicked, this, &RecorderWindow::chooseJob);
        connect(m_start, &QPushButton::clicked, this, &RecorderWindow::startNewSession);
        connect(m_recover, &QPushButton::clicked, this, [this] { ++m_segment; launchSegment(); });
        connect(m_finish, &QPushButton::clicked, this, &RecorderWindow::finishJob);
        connect(m_openSession, &QPushButton::clicked, this, [this] { QDesktopServices::openUrl(QUrl::fromLocalFile(m_session)); });
        connect(m_openCompleted, &QPushButton::clicked, this, [this] { QDesktopServices::openUrl(QUrl::fromLocalFile(m_jobRoot + QStringLiteral("/completed-sample"))); });
        connect(&m_editor, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this, &RecorderWindow::editorFinished);
        connect(&m_editor, &QProcess::started, this, [this] {
            writeSessionManifest(QStringLiteral("recording"));
            m_activity->appendPlainText(QStringLiteral("Kdenlive process started. Remote X11 startup can take 15–60 seconds."));
        });
        connect(&m_editor, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
            if (error == QProcess::FailedToStart) {
                writeSessionManifest(QStringLiteral("start_failed"));
                setStatus(QStringLiteral("Kdenlive could not be started. Check the SSH X11 connection and segment console log."), true);
                m_start->setEnabled(true);
            }
        });
        connect(&m_worker, &QProcess::readyReadStandardOutput, this, &RecorderWindow::readWorker);
        connect(&m_worker, &QProcess::readyReadStandardError, this, &RecorderWindow::readWorker);
        connect(&m_worker, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this, &RecorderWindow::workerFinished);
    }

    void setStatus(const QString &text, bool error = false)
    {
        m_status->setText(text);
        m_status->setStyleSheet(error ? QStringLiteral("padding:10px;background:#f7dddd;color:#7d1010;border-radius:4px;")
                                      : QStringLiteral("padding:10px;background:#e2f2e5;color:#164d24;border-radius:4px;"));
    }

    void chooseJob()
    {
        const QString file = QFileDialog::getOpenFileName(this, QStringLiteral("Open assigned job"), {}, QStringLiteral("Assigned jobs (job.json)"));
        if (!file.isEmpty()) loadJob(QFileInfo(file).absolutePath());
    }

    void loadJob(const QString &root)
    {
        QFile file(QDir(root).filePath(QStringLiteral("job.json")));
        if (!file.open(QIODevice::ReadOnly)) { setStatus(QStringLiteral("Could not read job.json."), true); return; }
        const auto document = QJsonDocument::fromJson(file.readAll());
        const auto job = document.object();
        const auto task = job.value(QStringLiteral("task")).toObject();
        const auto project = job.value(QStringLiteral("project")).toObject();
        const auto rate = project.value(QStringLiteral("frame_rate")).toObject();
        if (job.value(QStringLiteral("schema_version")).toString() != QStringLiteral("0.1.0")
            || job.value(QStringLiteral("job_id")).toString().isEmpty() || task.value(QStringLiteral("prompt")).toString().isEmpty()
            || job.value(QStringLiteral("assets")).toArray().isEmpty()) {
            setStatus(QStringLiteral("The selected job.json is incomplete or unsupported."), true); return;
        }
        QStringList clips;
        for (const auto &value : job.value(QStringLiteral("assets")).toArray()) {
            const QString relative = value.toObject().value(QStringLiteral("file")).toString();
            const QString absolute = QDir(root).filePath(relative);
            if (!QFileInfo::exists(absolute)) { setStatus(QStringLiteral("Assigned asset is missing: %1").arg(relative), true); return; }
            clips << QDir(absolute).absolutePath();
        }
        m_jobRoot = QDir(root).absolutePath(); m_assetPaths = clips;
        m_jobLabel->setText(QStringLiteral("%1 — %2").arg(job.value(QStringLiteral("job_id")).toString(), m_jobRoot));
        m_task->setText(QStringLiteral("<b>Task</b><br>%1<br><br><b>Project</b>: %2 × %3, %4/%5 fps &nbsp; <b>Assets</b>: %6")
            .arg(task.value(QStringLiteral("prompt")).toString().toHtmlEscaped())
            .arg(project.value(QStringLiteral("width")).toInt()).arg(project.value(QStringLiteral("height")).toInt())
            .arg(rate.value(QStringLiteral("numerator")).toInt()).arg(rate.value(QStringLiteral("denominator")).toInt()).arg(clips.size()));
        m_start->setEnabled(!QDir(m_jobRoot + QStringLiteral("/completed-sample")).exists());
        m_openCompleted->setEnabled(QDir(m_jobRoot + QStringLiteral("/completed-sample")).exists());
        setStatus(QStringLiteral("Assigned job is ready.")); QSettings().setValue(QStringLiteral("lastJob"), m_jobRoot);
        restoreLastSession();
    }

    void writeSessionManifest(const QString &status)
    {
        if (m_session.isEmpty()) return;
        QJsonObject manifest{{QStringLiteral("schema_version"), QStringLiteral("0.1.0")},
                             {QStringLiteral("job_root"), m_jobRoot}, {QStringLiteral("session_dir"), m_session},
                             {QStringLiteral("config_name"), m_configName}, {QStringLiteral("segment"), m_segment},
                             {QStringLiteral("status"), status}, {QStringLiteral("kdenlive_pid"), qint64(m_editor.processId())},
                             {QStringLiteral("updated_at_utc"), QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)}};
        QFile file(QDir(m_session).filePath(QStringLiteral("session.json")));
        if (file.open(QIODevice::WriteOnly | QIODevice::Truncate)) file.write(QJsonDocument(manifest).toJson(QJsonDocument::Indented));
        QSettings().setValue(QStringLiteral("lastSession"), m_session);
    }

    void restoreLastSession()
    {
        const QString previous = QSettings().value(QStringLiteral("lastSession")).toString();
        QFile file(QDir(previous).filePath(QStringLiteral("session.json")));
        if (previous.isEmpty() || !file.open(QIODevice::ReadOnly)) return;
        const auto manifest = QJsonDocument::fromJson(file.readAll()).object();
        if (manifest.value(QStringLiteral("job_root")).toString() != m_jobRoot) return;
        m_session = previous; m_configName = manifest.value(QStringLiteral("config_name")).toString();
        m_segment = manifest.value(QStringLiteral("segment")).toInt();
        m_sessionLabel->setText(m_session); m_openSession->setEnabled(true);
        const QString status = manifest.value(QStringLiteral("status")).toString();
        if (status == QStringLiteral("ready_to_finish")) {
            m_finish->setEnabled(true); setStatus(QStringLiteral("The previous recording is ready to finish."));
        } else if (status == QStringLiteral("recovery_available") || status == QStringLiteral("recording")) {
            m_recover->setVisible(true); setStatus(QStringLiteral("A previous session was interrupted. Verify Kdenlive is closed, then use Recover and Continue."), true);
        }
    }

    void startNewSession()
    {
        const QString stamp = QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd_HHmmss"));
        m_configName = QStringLiteral("edit-path-%1rc").arg(QUuid::createUuid().toString(QUuid::WithoutBraces).left(8));
        m_session = QDir(m_jobRoot).filePath(QStringLiteral("sessions/session_%1").arg(stamp));
        if (!QDir().mkpath(m_session)) { setStatus(QStringLiteral("Could not create session folder."), true); return; }
        m_segment = 1; m_sessionLabel->setText(m_session); m_openSession->setEnabled(true);
        writeSessionManifest(QStringLiteral("created"));
        launchSegment();
    }

    void launchSegment()
    {
        m_recover->setVisible(false); m_start->setEnabled(false); m_finish->setEnabled(false);
        const QString number = QStringLiteral("%1").arg(m_segment, 3, 10, QLatin1Char('0'));
        const QString raw = QDir(m_session).filePath(QStringLiteral("raw-events-%1.jsonl").arg(number));
        const QString console = QDir(m_session).filePath(QStringLiteral("kdenlive-console-%1.log").arg(number));
        QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_CONFIG"), m_configName);
        if (m_segment == 1) environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_CLIPS"), m_assetPaths.join(QLatin1Char(',')));
        else environment.remove(QStringLiteral("KDENLIVE_VIDEO_PATH_CLIPS"));
        m_editor.setProcessEnvironment(environment); m_editor.setWorkingDirectory(m_repoRoot);
        m_editor.setProcessChannelMode(QProcess::MergedChannels); m_editor.setStandardOutputFile(console, QIODevice::Append);
        m_activity->appendPlainText(QStringLiteral("Starting recording segment %1…").arg(number));
        setStatus(m_segment == 1 ? QStringLiteral("Editing session is recording. Save the project and final render in the session folder.")
                                 : QStringLiteral("Recovery segment is recording. Complete the edit and close Kdenlive normally."));
        m_editor.start(m_repoRoot + QStringLiteral("/video-path-pilot/run-video-path-pilot.sh"), {raw});
    }

    void editorFinished(int exitCode, QProcess::ExitStatus status)
    {
        m_activity->appendPlainText(QStringLiteral("Kdenlive exited with code %1; checking recording…").arg(exitCode));
        m_workerPurpose = QStringLiteral("validate-segment");
        const QString raw = QDir(m_session).filePath(QStringLiteral("raw-events-%1.jsonl").arg(m_segment, 3, 10, QLatin1Char('0')));
        m_worker.start(QStringLiteral("python3"), {m_repoRoot + QStringLiteral("/video-path-pilot/validate_video_path.py"), raw});
        Q_UNUSED(status)
    }

    void finishJob()
    {
        m_finish->setEnabled(false); m_workerPurpose = QStringLiteral("finalize");
        setStatus(QStringLiteral("Resolving assets, normalizing the edit path, generating sample.json, and replaying canonical state…"));
        m_worker.start(QStringLiteral("python3"), {m_repoRoot + QStringLiteral("/video-path-pilot/job_pipeline.py"),
                       QStringLiteral("finalize"), m_jobRoot, m_session});
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
        readWorker(); const bool success = status == QProcess::NormalExit && exitCode == 0;
        if (m_workerPurpose == QStringLiteral("validate-segment")) {
            if (success) {
                writeSessionManifest(QStringLiteral("ready_to_finish"));
                setStatus(QStringLiteral("Recording completed. Ensure exactly one .kdenlive project and one rendered video are in the session folder, then click Finish Job."));
                m_finish->setEnabled(true); m_start->setEnabled(true);
            } else {
                writeSessionManifest(QStringLiteral("recovery_available"));
                setStatus(QStringLiteral("Kdenlive did not close cleanly. Use Recover and Continue to reopen the isolated session and Kdenlive recovery."), true);
                m_recover->setVisible(true);
            }
        } else if (m_workerPurpose == QStringLiteral("finalize")) {
            if (success) {
                writeSessionManifest(QStringLiteral("packaged"));
                m_openCompleted->setEnabled(true); m_start->setEnabled(false);
                QFile reportFile(m_jobRoot + QStringLiteral("/completed-sample/validation/reconstruction-report.json"));
                bool mediaPassed = false;
                if (reportFile.open(QIODevice::ReadOnly)) {
                    const auto report = QJsonDocument::fromJson(reportFile.readAll()).object();
                    mediaPassed = report.value(QStringLiteral("media_project_reconstruction")).toString() == QStringLiteral("passed");
                }
                if (mediaPassed) {
                    setStatus(QStringLiteral("Sample generated. Canonical replay, reconstructed render, and media comparison passed."));
                    QMessageBox::information(this, QStringLiteral("Job complete"), QStringLiteral("The sample and reconstructed render passed validation."));
                } else {
                    setStatus(QStringLiteral("Sample generated and canonical replay passed, but this edit uses media features the reconstruction adapter does not yet support. It is not ready for client review."), true);
                    QMessageBox::warning(this, QStringLiteral("Reconstruction pending"), QStringLiteral("The sample was packaged, but media reconstruction did not pass. See reconstruction-report.json."));
                }
            } else {
                setStatus(QStringLiteral("Job packaging failed. Review Activity, correct the project/render/assets, and try Finish Job again."), true);
                m_finish->setEnabled(true);
            }
        }
        m_workerPurpose.clear();
    }

    QString m_repoRoot, m_jobRoot, m_session, m_configName, m_workerPurpose;
    QStringList m_assetPaths; int m_segment{0};
    QProcess m_editor, m_worker;
    QLabel *m_jobLabel{}, *m_task{}, *m_status{}, *m_sessionLabel{};
    QPushButton *m_openJob{}, *m_start{}, *m_recover{}, *m_finish{}, *m_openSession{}, *m_openCompleted{};
    QPlainTextEdit *m_activity{};
};

int main(int argc, char **argv)
{
    QApplication application(argc, argv);
    QCoreApplication::setOrganizationName(QStringLiteral("Parsewave"));
    QCoreApplication::setApplicationName(QStringLiteral("EditPathRecorder"));
    RecorderWindow window; window.show(); return application.exec();
}
