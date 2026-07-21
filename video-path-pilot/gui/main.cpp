// SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
// SPDX-License-Identifier: GPL-3.0-only

#include <QApplication>
#include <QCloseEvent>
#include <QDir>
#include <QFileDialog>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMainWindow>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QProcess>
#include <QPushButton>
#include <QSettings>
#include <QSpinBox>
#include <QStandardPaths>
#include <QTextEdit>
#include <QVBoxLayout>

namespace {
QString repositoryRoot()
{
    const QString configured = qEnvironmentVariable("EDIT_PATH_REPO_ROOT");
    if (!configured.isEmpty() && QFileInfo::exists(configured + QStringLiteral("/video-path-pilot/sample_collector.py"))) {
        return QDir(configured).absolutePath();
    }
    QDir current(QCoreApplication::applicationDirPath());
    for (int depth = 0; depth < 6; ++depth) {
        if (QFileInfo::exists(current.filePath(QStringLiteral("video-path-pilot/sample_collector.py")))) {
            return current.absolutePath();
        }
        if (!current.cdUp()) {
            break;
        }
    }
    return {};
}

QTextEdit *paragraphEditor(const QString &placeholder)
{
    auto *editor = new QTextEdit;
    editor->setPlaceholderText(placeholder);
    editor->setMinimumHeight(72);
    return editor;
}
}

class CollectorWindow final : public QMainWindow
{
public:
    CollectorWindow()
        : m_repoRoot(repositoryRoot())
    {
        setWindowTitle(QStringLiteral("Edit Path Collector MVP"));
        resize(900, 760);
        buildUi();
        QSettings settings;
        const QString previous = settings.value(QStringLiteral("currentSample")).toString();
        if (!previous.isEmpty() && QFileInfo::exists(previous + QStringLiteral("/internal/collector-metadata.json"))) {
            selectSample(previous);
        }
        if (m_repoRoot.isEmpty()) {
            showError(QStringLiteral("Collector files could not be found. Start the app with the supplied launcher."));
            setControlsEnabled(false);
        }
    }

protected:
    void closeEvent(QCloseEvent *event) override
    {
        if (m_process.state() != QProcess::NotRunning || m_editorProcess.state() != QProcess::NotRunning) {
            QMessageBox::warning(this, QStringLiteral("Kdenlive is running"),
                                 QStringLiteral("Close Kdenlive before closing the collector."));
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
        auto *title = new QLabel(QStringLiteral("<h1>Edit Path Collector</h1><p>Create and package training samples without using a terminal.</p>"));
        title->setTextFormat(Qt::RichText);
        layout->addWidget(title);

        auto *workspaceRow = new QHBoxLayout;
        m_workspaceLabel = new QLabel(QStringLiteral("No sample selected"));
        m_workspaceLabel->setTextInteractionFlags(Qt::TextSelectableByMouse);
        auto *newButton = new QPushButton(QStringLiteral("Create New Sample"));
        auto *openButton = new QPushButton(QStringLiteral("Open Existing Sample"));
        workspaceRow->addWidget(m_workspaceLabel, 1);
        workspaceRow->addWidget(newButton);
        workspaceRow->addWidget(openButton);
        layout->addLayout(workspaceRow);

        m_newSample = new QGroupBox(QStringLiteral("1. Sample setup"));
        auto *form = new QFormLayout(m_newSample);
        m_sampleDirectory = new QLineEdit;
        auto *directoryRow = new QHBoxLayout;
        auto *chooseDirectory = new QPushButton(QStringLiteral("Choose…"));
        directoryRow->addWidget(m_sampleDirectory, 1);
        directoryRow->addWidget(chooseDirectory);
        form->addRow(QStringLiteral("New sample folder"), directoryRow);
        m_editorId = new QLineEdit(QStringLiteral("editor_001"));
        form->addRow(QStringLiteral("Editor ID"), m_editorId);
        m_prompt = paragraphEditor(QStringLiteral("What should the finished video accomplish?"));
        form->addRow(QStringLiteral("Editing prompt"), m_prompt);
        m_plan = paragraphEditor(QStringLiteral("Describe your intended structure, pacing, audio, and finish."));
        form->addRow(QStringLiteral("Initial plan"), m_plan);
        auto *profileRow = new QHBoxLayout;
        m_width = new QSpinBox; m_width->setRange(1, 16384); m_width->setValue(1920);
        m_height = new QSpinBox; m_height->setRange(1, 16384); m_height->setValue(1080);
        m_fpsNumerator = new QSpinBox; m_fpsNumerator->setRange(1, 240000); m_fpsNumerator->setValue(25);
        m_fpsDenominator = new QSpinBox; m_fpsDenominator->setRange(1, 1001); m_fpsDenominator->setValue(1);
        profileRow->addWidget(new QLabel(QStringLiteral("Width"))); profileRow->addWidget(m_width);
        profileRow->addWidget(new QLabel(QStringLiteral("Height"))); profileRow->addWidget(m_height);
        profileRow->addWidget(new QLabel(QStringLiteral("FPS"))); profileRow->addWidget(m_fpsNumerator);
        profileRow->addWidget(new QLabel(QStringLiteral("/"))); profileRow->addWidget(m_fpsDenominator);
        form->addRow(QStringLiteral("Project profile"), profileRow);
        m_assets = new QListWidget; m_assets->setMinimumHeight(90);
        auto *assetButtons = new QHBoxLayout;
        auto *addAssets = new QPushButton(QStringLiteral("Add Asset Files…"));
        auto *removeAsset = new QPushButton(QStringLiteral("Remove Selected"));
        assetButtons->addWidget(addAssets); assetButtons->addWidget(removeAsset); assetButtons->addStretch();
        form->addRow(QStringLiteral("Source assets"), m_assets);
        form->addRow(QString(), assetButtons);
        auto *createButton = new QPushButton(QStringLiteral("Create Sample"));
        form->addRow(QString(), createButton);
        layout->addWidget(m_newSample);

        m_workflow = new QGroupBox(QStringLiteral("2. Edit and annotate"));
        auto *workflowLayout = new QVBoxLayout(m_workflow);
        m_status = new QLabel(QStringLiteral("Create or open a sample to begin."));
        m_status->setWordWrap(true);
        workflowLayout->addWidget(m_status);
        auto *launchButton = new QPushButton(QStringLiteral("Launch Instrumented Kdenlive"));
        workflowLayout->addWidget(launchButton);
        auto *noteForm = new QFormLayout;
        m_reason = new QLineEdit; m_reason->setPlaceholderText(QStringLiteral("Why was a decision needed?"));
        m_decision = new QLineEdit; m_decision->setPlaceholderText(QStringLiteral("What did you decide to do?"));
        auto *saveNote = new QPushButton(QStringLiteral("Save Creative Decision"));
        noteForm->addRow(QStringLiteral("Reason"), m_reason);
        noteForm->addRow(QStringLiteral("Decision"), m_decision);
        noteForm->addRow(QString(), saveNote);
        workflowLayout->addLayout(noteForm);
        layout->addWidget(m_workflow);

        m_finalize = new QGroupBox(QStringLiteral("3. Finalize sample"));
        auto *finalForm = new QFormLayout(m_finalize);
        m_projectFile = new QLineEdit;
        m_outputFile = new QLineEdit;
        auto addPicker = [this, finalForm](const QString &label, QLineEdit *field, const QString &filter) {
            auto *row = new QHBoxLayout;
            auto *button = new QPushButton(QStringLiteral("Choose…"));
            row->addWidget(field, 1); row->addWidget(button);
            finalForm->addRow(label, row);
            connect(button, &QPushButton::clicked, this, [this, field, filter] {
                const QString file = QFileDialog::getOpenFileName(this, QStringLiteral("Choose file"), {}, filter);
                if (!file.isEmpty()) field->setText(file);
            });
        };
        addPicker(QStringLiteral("Saved Kdenlive project"), m_projectFile, QStringLiteral("Kdenlive projects (*.kdenlive);;All files (*)"));
        addPicker(QStringLiteral("Rendered final video"), m_outputFile, QStringLiteral("Video files (*.mp4 *.mov *.mkv *.webm);;All files (*)"));
        m_review = paragraphEditor(QStringLiteral("Confirm how the result follows the prompt and what you checked."));
        finalForm->addRow(QStringLiteral("Final editor review"), m_review);
        auto *finishRow = new QHBoxLayout;
        auto *finalizeButton = new QPushButton(QStringLiteral("Finalize and Validate"));
        auto *validateButton = new QPushButton(QStringLiteral("Validate Existing Sample"));
        finishRow->addWidget(finalizeButton); finishRow->addWidget(validateButton); finishRow->addStretch();
        finalForm->addRow(QString(), finishRow);
        layout->addWidget(m_finalize);

        m_log = new QPlainTextEdit; m_log->setReadOnly(true); m_log->setMaximumBlockCount(500); m_log->setMinimumHeight(100);
        layout->addWidget(new QLabel(QStringLiteral("Activity")));
        layout->addWidget(m_log);
        setCentralWidget(central);
        setControlsEnabled(false);

        connect(newButton, &QPushButton::clicked, this, [this] { m_newSample->setVisible(true); });
        connect(openButton, &QPushButton::clicked, this, &CollectorWindow::openSample);
        connect(chooseDirectory, &QPushButton::clicked, this, [this] {
            const QString parent = QFileDialog::getExistingDirectory(this, QStringLiteral("Choose parent folder"));
            if (!parent.isEmpty()) m_sampleDirectory->setText(QDir(parent).filePath(QStringLiteral("sample_001")));
        });
        connect(addAssets, &QPushButton::clicked, this, [this] {
            const QStringList files = QFileDialog::getOpenFileNames(this, QStringLiteral("Choose source assets"));
            for (const QString &file : files) if (m_assets->findItems(file, Qt::MatchExactly).isEmpty()) m_assets->addItem(file);
        });
        connect(removeAsset, &QPushButton::clicked, this, [this] { qDeleteAll(m_assets->selectedItems()); });
        connect(createButton, &QPushButton::clicked, this, &CollectorWindow::createSample);
        connect(launchButton, &QPushButton::clicked, this, &CollectorWindow::launchEditor);
        connect(saveNote, &QPushButton::clicked, this, &CollectorWindow::saveDecision);
        connect(finalizeButton, &QPushButton::clicked, this, &CollectorWindow::finalizeSample);
        connect(validateButton, &QPushButton::clicked, this, [this] { runCollector({QStringLiteral("validate"), m_currentSample}); });
        connect(&m_process, &QProcess::readyReadStandardOutput, this, &CollectorWindow::readProcessOutput);
        connect(&m_process, &QProcess::readyReadStandardError, this, &CollectorWindow::readProcessOutput);
        connect(&m_process, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this, &CollectorWindow::processFinished);
        connect(&m_editorProcess, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this, &CollectorWindow::editorFinished);
    }

    void setControlsEnabled(bool enabled)
    {
        m_workflow->setEnabled(enabled);
        m_finalize->setEnabled(enabled);
    }

    void showError(const QString &message) { QMessageBox::critical(this, QStringLiteral("Edit Path Collector"), message); }

    void selectSample(const QString &path)
    {
        const QString absolute = QDir(path).absolutePath();
        if (!QFileInfo::exists(absolute + QStringLiteral("/internal/collector-metadata.json"))) {
            showError(QStringLiteral("This folder is not a collector sample."));
            return;
        }
        m_currentSample = absolute;
        m_workspaceLabel->setText(absolute);
        m_status->setText(QStringLiteral("Sample ready. Launch Kdenlive, import assets in filename order, edit, save, render, and close normally."));
        setControlsEnabled(true);
        m_newSample->setVisible(false);
        QSettings().setValue(QStringLiteral("currentSample"), absolute);
    }

    void openSample()
    {
        const QString directory = QFileDialog::getExistingDirectory(this, QStringLiteral("Open sample folder"));
        if (!directory.isEmpty()) selectSample(directory);
    }

    void createSample()
    {
        if (m_sampleDirectory->text().trimmed().isEmpty() || m_editorId->text().trimmed().isEmpty()
            || m_prompt->toPlainText().trimmed().isEmpty() || m_plan->toPlainText().trimmed().isEmpty() || m_assets->count() == 0) {
            showError(QStringLiteral("Folder, editor ID, prompt, plan, and at least one asset are required."));
            return;
        }
        QStringList arguments{QStringLiteral("init"), m_sampleDirectory->text().trimmed(),
                              QStringLiteral("--editor-id"), m_editorId->text().trimmed(),
                              QStringLiteral("--prompt"), m_prompt->toPlainText().trimmed(),
                              QStringLiteral("--plan"), m_plan->toPlainText().trimmed(),
                              QStringLiteral("--fps-num"), QString::number(m_fpsNumerator->value()),
                              QStringLiteral("--fps-den"), QString::number(m_fpsDenominator->value()),
                              QStringLiteral("--width"), QString::number(m_width->value()),
                              QStringLiteral("--height"), QString::number(m_height->value())};
        for (int i = 0; i < m_assets->count(); ++i) arguments << m_assets->item(i)->text();
        m_pendingSample = QDir(m_sampleDirectory->text().trimmed()).absolutePath();
        runCollector(arguments, QStringLiteral("init"));
    }

    void launchEditor()
    {
        if (m_editorProcess.state() != QProcess::NotRunning) {
            showError(QStringLiteral("Kdenlive is already running for this sample."));
            return;
        }
        if (QFileInfo::exists(m_currentSample + QStringLiteral("/evidence/raw-events.jsonl"))) {
            showError(QStringLiteral("This sample already has a recording. Open or create a fresh sample instead of overwriting evidence."));
            return;
        }
        m_status->setText(QStringLiteral("Kdenlive is running. Keep this collector open and close Kdenlive normally when finished."));
        QStringList arguments{m_repoRoot + QStringLiteral("/video-path-pilot/sample_collector.py"),
                              QStringLiteral("launch"), m_currentSample};
        m_log->appendPlainText(QStringLiteral("Launching instrumented Kdenlive…"));
        m_editorProcess.setWorkingDirectory(m_repoRoot);
        m_editorProcess.setProcessChannelMode(QProcess::MergedChannels);
        m_editorProcess.setStandardOutputFile(m_currentSample + QStringLiteral("/internal/kdenlive-console.log"), QIODevice::Append);
        m_editorProcess.start(QStringLiteral("python3"), arguments);
    }

    void saveDecision()
    {
        if (m_reason->text().trimmed().isEmpty() || m_decision->text().trimmed().isEmpty()) {
            showError(QStringLiteral("Both the reason and decision are required."));
            return;
        }
        runCollector({QStringLiteral("note"), m_currentSample, QStringLiteral("--reason"), m_reason->text().trimmed(),
                      QStringLiteral("--decision"), m_decision->text().trimmed()}, QStringLiteral("note"));
    }

    void finalizeSample()
    {
        if (m_editorProcess.state() != QProcess::NotRunning) {
            showError(QStringLiteral("Close Kdenlive normally before finalizing the sample."));
            return;
        }
        if (m_projectFile->text().trimmed().isEmpty() || m_outputFile->text().trimmed().isEmpty()
            || m_review->toPlainText().trimmed().isEmpty()) {
            showError(QStringLiteral("Choose the saved project and final video, then provide the final review."));
            return;
        }
        runCollector({QStringLiteral("finalize"), m_currentSample,
                      QStringLiteral("--project"), m_projectFile->text().trimmed(),
                      QStringLiteral("--output"), m_outputFile->text().trimmed(),
                      QStringLiteral("--review"), m_review->toPlainText().trimmed()}, QStringLiteral("finalize"));
    }

    void runCollector(QStringList arguments, const QString &purpose = {})
    {
        if (m_process.state() != QProcess::NotRunning) {
            showError(QStringLiteral("Another collector task is still running."));
            return;
        }
        m_purpose = purpose;
        arguments.prepend(m_repoRoot + QStringLiteral("/video-path-pilot/sample_collector.py"));
        m_log->appendPlainText(QStringLiteral("Starting %1…").arg(purpose.isEmpty() ? arguments.value(1) : purpose));
        m_process.setWorkingDirectory(m_repoRoot);
        m_process.start(QStringLiteral("python3"), arguments);
    }

    void readProcessOutput()
    {
        const QString standard = QString::fromUtf8(m_process.readAllStandardOutput()).trimmed();
        const QString errors = QString::fromUtf8(m_process.readAllStandardError()).trimmed();
        if (!standard.isEmpty()) m_log->appendPlainText(standard);
        if (!errors.isEmpty()) m_log->appendPlainText(errors);
    }

    void processFinished(int exitCode, QProcess::ExitStatus status)
    {
        readProcessOutput();
        const bool success = status == QProcess::NormalExit && exitCode == 0;
        m_log->appendPlainText(success ? QStringLiteral("Completed successfully.") : QStringLiteral("Task failed (exit code %1).").arg(exitCode));
        if (success && m_purpose == QStringLiteral("init")) selectSample(m_pendingSample);
        if (success && m_purpose == QStringLiteral("note")) { m_reason->clear(); m_decision->clear(); }
        if (success && m_purpose == QStringLiteral("finalize")) {
            m_status->setText(QStringLiteral("Sample finalized and validated. It is ready for human review."));
            QMessageBox::information(this, QStringLiteral("Sample complete"), QStringLiteral("sample.json and all required evidence were created successfully."));
        } else if (!success && m_purpose != QStringLiteral("launch")) {
            showError(QStringLiteral("The task failed. See Activity for details."));
        }
        m_purpose.clear();
    }

    void editorFinished(int exitCode, QProcess::ExitStatus status)
    {
        const bool success = status == QProcess::NormalExit && exitCode == 0;
        m_log->appendPlainText(success ? QStringLiteral("Kdenlive closed normally.")
                                       : QStringLiteral("Kdenlive exited unexpectedly (code %1).").arg(exitCode));
        m_status->setText(success ? QStringLiteral("Kdenlive closed normally. Select the saved project and render to finalize.")
                                  : QStringLiteral("Kdenlive did not close cleanly. This recording may be incomplete."));
    }

    QString m_repoRoot;
    QString m_currentSample;
    QString m_pendingSample;
    QString m_purpose;
    QProcess m_process;
    QProcess m_editorProcess;
    QLabel *m_workspaceLabel{};
    QLabel *m_status{};
    QGroupBox *m_newSample{};
    QGroupBox *m_workflow{};
    QGroupBox *m_finalize{};
    QLineEdit *m_sampleDirectory{};
    QLineEdit *m_editorId{};
    QTextEdit *m_prompt{};
    QTextEdit *m_plan{};
    QSpinBox *m_width{};
    QSpinBox *m_height{};
    QSpinBox *m_fpsNumerator{};
    QSpinBox *m_fpsDenominator{};
    QListWidget *m_assets{};
    QLineEdit *m_reason{};
    QLineEdit *m_decision{};
    QLineEdit *m_projectFile{};
    QLineEdit *m_outputFile{};
    QTextEdit *m_review{};
    QPlainTextEdit *m_log{};
};

int main(int argc, char **argv)
{
    QApplication application(argc, argv);
    QCoreApplication::setOrganizationName(QStringLiteral("Parsewave"));
    QCoreApplication::setApplicationName(QStringLiteral("EditPathCollector"));
    CollectorWindow window;
    window.show();
    return application.exec();
}
