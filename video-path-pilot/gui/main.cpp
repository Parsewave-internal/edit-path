// SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
// SPDX-License-Identifier: GPL-3.0-only

#include <QApplication>
#include <QCloseEvent>
#include <QDateTime>
#include <QDesktopServices>
#include <QDir>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QLabel>
#include <QMainWindow>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QProcess>
#include <QPushButton>
#include <QSettings>
#include <QStandardPaths>
#include <QUrl>
#include <QUuid>
#include <QVBoxLayout>

namespace {
QString repositoryRoot()
{
    const QString configured = qEnvironmentVariable("EDIT_PATH_REPO_ROOT");
    if (!configured.isEmpty() && QFileInfo::exists(configured + QStringLiteral("/video-path-pilot/run-video-path-pilot.sh"))) {
        return QDir(configured).absolutePath();
    }
    QDir current(QCoreApplication::applicationDirPath());
    for (int depth = 0; depth < 6; ++depth) {
        if (QFileInfo::exists(current.filePath(QStringLiteral("video-path-pilot/run-video-path-pilot.sh")))) {
            return current.absolutePath();
        }
        if (!current.cdUp()) break;
    }
    return {};
}

QString defaultSessionRoot()
{
    QString videos = QStandardPaths::writableLocation(QStandardPaths::MoviesLocation);
    if (videos.isEmpty()) videos = QDir::homePath() + QStringLiteral("/Videos");
    return QDir(videos).filePath(QStringLiteral("EditPathSessions"));
}
}

class RecorderWindow final : public QMainWindow
{
public:
    RecorderWindow()
        : m_repoRoot(repositoryRoot())
    {
        setWindowTitle(QStringLiteral("Edit Path Recorder MVP"));
        resize(720, 520);
        buildUi();
        const QString previous = QSettings().value(QStringLiteral("lastSession")).toString();
        if (!previous.isEmpty() && QDir(previous).exists()) setSession(previous);
        if (m_repoRoot.isEmpty()) {
            m_startButton->setEnabled(false);
            setStatus(QStringLiteral("Recorder installation was not found."), true);
        }
    }

protected:
    void closeEvent(QCloseEvent *event) override
    {
        if (m_editor.state() != QProcess::NotRunning || m_validator.state() != QProcess::NotRunning) {
            QMessageBox::warning(this, QStringLiteral("Recording active"),
                                 QStringLiteral("Close Kdenlive normally before closing the recorder."));
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
        auto *title = new QLabel(QStringLiteral(
            "<h1>Edit Path Recorder</h1>"
            "<p>This application records editing actions and timeline outcomes. "
            "It does not ask for plans, explanations, decisions, or other editor intent.</p>"));
        title->setWordWrap(true);
        layout->addWidget(title);

        auto *instructions = new QLabel(QStringLiteral(
            "<b>Editor instructions</b><ol>"
            "<li>Click <b>Start Editing Session</b>.</li>"
            "<li>Use the fresh Kdenlive window and edit normally with the assets supplied for the task.</li>"
            "<li>Save the Kdenlive project and rendered video in the session folder shown below.</li>"
            "<li>Close Kdenlive normally and wait for recording validation.</li>"
            "<li>Return the complete session folder to the project team.</li>"
            "</ol>"));
        instructions->setWordWrap(true);
        layout->addWidget(instructions);

        m_status = new QLabel;
        m_status->setWordWrap(true);
        m_status->setStyleSheet(QStringLiteral("padding: 10px; border-radius: 4px; background: #e8eef7;"));
        setStatus(QStringLiteral("Ready to start a new editing session."));
        layout->addWidget(m_status);

        auto *pathTitle = new QLabel(QStringLiteral("<b>Current session folder</b>"));
        layout->addWidget(pathTitle);
        m_sessionPath = new QLabel(QStringLiteral("No session created yet"));
        m_sessionPath->setTextInteractionFlags(Qt::TextSelectableByMouse);
        m_sessionPath->setWordWrap(true);
        layout->addWidget(m_sessionPath);

        auto *buttons = new QHBoxLayout;
        m_startButton = new QPushButton(QStringLiteral("Start Editing Session"));
        m_startButton->setMinimumHeight(42);
        m_openButton = new QPushButton(QStringLiteral("Open Session Folder"));
        m_openButton->setEnabled(false);
        buttons->addWidget(m_startButton);
        buttons->addWidget(m_openButton);
        layout->addLayout(buttons);

        m_activity = new QPlainTextEdit;
        m_activity->setReadOnly(true);
        m_activity->setMaximumBlockCount(200);
        m_activity->setPlaceholderText(QStringLiteral("Recording activity will appear here."));
        layout->addWidget(m_activity, 1);
        setCentralWidget(central);

        connect(m_startButton, &QPushButton::clicked, this, &RecorderWindow::startSession);
        connect(m_openButton, &QPushButton::clicked, this, [this] {
            QDesktopServices::openUrl(QUrl::fromLocalFile(m_currentSession));
        });
        connect(&m_editor, qOverload<int, QProcess::ExitStatus>(&QProcess::finished),
                this, &RecorderWindow::editorFinished);
        connect(&m_validator, &QProcess::readyReadStandardOutput, this, &RecorderWindow::readValidatorOutput);
        connect(&m_validator, &QProcess::readyReadStandardError, this, &RecorderWindow::readValidatorOutput);
        connect(&m_validator, qOverload<int, QProcess::ExitStatus>(&QProcess::finished),
                this, &RecorderWindow::validatorFinished);
    }

    void setStatus(const QString &message, bool error = false)
    {
        m_status->setText(message);
        m_status->setStyleSheet(error
            ? QStringLiteral("padding: 10px; border-radius: 4px; background: #f7dddd; color: #7d1010;")
            : QStringLiteral("padding: 10px; border-radius: 4px; background: #e2f2e5; color: #164d24;"));
    }

    void setSession(const QString &path)
    {
        m_currentSession = QDir(path).absolutePath();
        m_sessionPath->setText(m_currentSession);
        m_openButton->setEnabled(true);
        QSettings().setValue(QStringLiteral("lastSession"), m_currentSession);
    }

    void startSession()
    {
        if (m_editor.state() != QProcess::NotRunning) return;
        const QString stamp = QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd_HHmmss"));
        const QString suffix = QUuid::createUuid().toString(QUuid::WithoutBraces).left(8);
        const QString path = QDir(defaultSessionRoot()).filePath(QStringLiteral("session_%1_%2").arg(stamp, suffix));
        QDir directory;
        if (!directory.mkpath(path)) {
            setStatus(QStringLiteral("Could not create the session folder: %1").arg(path), true);
            return;
        }
        setSession(path);
        const QString raw = QDir(path).filePath(QStringLiteral("raw-events.jsonl"));
        const QString console = QDir(path).filePath(QStringLiteral("kdenlive-console.log"));
        const QString configName = QStringLiteral("edit-path-%1rc").arg(suffix);

        m_activity->appendPlainText(QStringLiteral("Session created: %1").arg(path));
        m_activity->appendPlainText(QStringLiteral("Launching a fresh isolated Kdenlive session…"));
        setStatus(QStringLiteral("Recording in progress. Save the project and rendered video in the session folder, then close Kdenlive normally."));
        m_startButton->setEnabled(false);

        QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
        environment.insert(QStringLiteral("KDENLIVE_VIDEO_PATH_CONFIG"), configName);
        m_editor.setProcessEnvironment(environment);
        m_editor.setWorkingDirectory(m_repoRoot);
        m_editor.setProcessChannelMode(QProcess::MergedChannels);
        m_editor.setStandardOutputFile(console, QIODevice::Append);
        m_editor.start(m_repoRoot + QStringLiteral("/video-path-pilot/run-video-path-pilot.sh"), {raw});
        if (!m_editor.waitForStarted(5000)) {
            m_startButton->setEnabled(true);
            setStatus(QStringLiteral("Kdenlive could not be started. See kdenlive-console.log."), true);
        }
    }

    void editorFinished(int exitCode, QProcess::ExitStatus status)
    {
        m_activity->appendPlainText(QStringLiteral("Kdenlive exited; validating the recording…"));
        if (status != QProcess::NormalExit || exitCode != 0) {
            m_activity->appendPlainText(QStringLiteral("Kdenlive exit code: %1").arg(exitCode));
        }
        const QString validator = m_repoRoot + QStringLiteral("/video-path-pilot/validate_video_path.py");
        const QString raw = QDir(m_currentSession).filePath(QStringLiteral("raw-events.jsonl"));
        m_validator.setWorkingDirectory(m_repoRoot);
        m_validator.start(QStringLiteral("python3"), {validator, raw});
    }

    void readValidatorOutput()
    {
        const QString output = QString::fromUtf8(m_validator.readAllStandardOutput()).trimmed();
        const QString errors = QString::fromUtf8(m_validator.readAllStandardError()).trimmed();
        if (!output.isEmpty()) m_activity->appendPlainText(output);
        if (!errors.isEmpty()) m_activity->appendPlainText(errors);
    }

    void validatorFinished(int exitCode, QProcess::ExitStatus status)
    {
        readValidatorOutput();
        m_startButton->setEnabled(true);
        if (status == QProcess::NormalExit && exitCode == 0) {
            setStatus(QStringLiteral("Recording completed successfully. Return this session folder, the saved project, rendered video, and source assets to the project team."));
            QMessageBox::information(this, QStringLiteral("Recording complete"),
                                     QStringLiteral("The editing session was recorded and validated successfully."));
        } else {
            setStatus(QStringLiteral("Recording is incomplete or invalid. Kdenlive may have crashed or been force-quit. Do not submit this session as complete."), true);
            QMessageBox::warning(this, QStringLiteral("Recording incomplete"),
                                 QStringLiteral("The session did not pass validation. See Activity and kdenlive-console.log."));
        }
    }

    QString m_repoRoot;
    QString m_currentSession;
    QProcess m_editor;
    QProcess m_validator;
    QLabel *m_status{};
    QLabel *m_sessionPath{};
    QPushButton *m_startButton{};
    QPushButton *m_openButton{};
    QPlainTextEdit *m_activity{};
};

int main(int argc, char **argv)
{
    QApplication application(argc, argv);
    QCoreApplication::setOrganizationName(QStringLiteral("Parsewave"));
    QCoreApplication::setApplicationName(QStringLiteral("EditPathRecorder"));
    RecorderWindow window;
    window.show();
    return application.exec();
}
