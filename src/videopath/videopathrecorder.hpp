/*
    SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
    SPDX-License-Identifier: GPL-3.0-only
*/

#pragma once

#include <QElapsedTimer>
#include <QFuture>
#include <QHash>
#include <QJsonObject>
#include <QMutex>
#include <QObject>
#include <QPointF>
#include <QSet>
#include <QString>
#include <QThreadPool>

#include <functional>
#include <memory>

class QFile;
class QAction;
class QApplication;
class QKeyEvent;
class QKeySequence;

/** Append-only recorder for software-independent Video Path events. */
class VideoPathRecorder : public QObject
{
public:
    static VideoPathRecorder &instance();

    void initialize(QApplication *application);
    bool isEnabled() const;
    void beginTransaction(const QString &boundary, const QString &label, const QString &undoEntryKey);
    void bindTransactionUndoEntry(const QString &undoEntryKey);
    void forgetUndoEntry(const QString &undoEntryKey);
    void resetUndoEntries();
    void endTransaction();
    void recordAction(const QString &action, const QString &timelineId, const QJsonObject &parameters);
    void recordHistory(const QString &operation, const QString &label);
    void setProjectStateProvider(std::function<QByteArray()> provider);
    void recordProjectContext(const QJsonObject &context);
    void recordLifecycle(const QString &eventType, const QString &reason, const QJsonObject &details = {});
    void captureTimelineCheckpoint(const QString &label);
    void captureTimelineChange(const QString &label, const QString &boundary);

private:
    struct PendingSidecarWrite {
        QFuture<bool> future;
        QString stateHash;
        QString checkpointHash;
    };

    VideoPathRecorder();
    ~VideoPathRecorder();
    Q_DISABLE_COPY_MOVE(VideoPathRecorder)

    void writeEvent(QJsonObject event);
    void writeSessionStart();
    void writeSessionEnd();
    void scheduleActionDiscovery();
    void attachActions();
    void recordCommand(QAction *action, bool checked);
    void recordShortcut(const QKeySequence &sequence, bool ambiguous);
    QJsonObject currentTimelineSnapshot() const;
    QJsonObject projectStateReference(QByteArray *rawState = nullptr);
    QJsonObject scheduleCheckpointProxy(const QByteArray &projectState);
    void flushPendingActions(bool attachTransaction);
    void addTransactionFields(QJsonObject &event, bool allowLastCompleted = false) const;
    QString stableEntityId(const QString &kind, const QString &nativeId) const;
    void persistEntityMap() const;
    bool waitForStateSidecars();
    void reapFinishedStateSidecars(bool waitForSlot);
    static QJsonObject diffSnapshots(const QJsonObject &before, const QJsonObject &after);
    bool eventFilter(QObject *watched, QEvent *event) override;
    static QString describeObject(const QObject *object);
    static bool isTimelineCanvasTarget(const QObject *object);
    static bool hasMenuAncestor(const QObject *object);
    static bool hasToolButtonAncestor(const QObject *object);

    mutable QMutex m_mutex;
    std::unique_ptr<QFile> m_file;
    QString m_logDirectory;
    QString m_stateDirectory;
    QString m_entityMapPath;
    QString m_sessionId;
    qint64 m_sequence{0};
    QSet<QAction *> m_actions;
    bool m_actionDiscoveryScheduled{false};
    QElapsedTimer m_lastShortcut;
    QElapsedTimer m_lastInteraction;
    QElapsedTimer m_lastMenuClick;
    QElapsedTimer m_lastToolbarClick;
    QString m_lastInputInteractionId;
    QString m_lastShortcutSequence;
    QString m_pointerInteractionId;
    QPointF m_pointerStart;
    QString m_pointerTarget;
    int m_pointerButton{0};
    QHash<QString, QJsonObject> m_lastSnapshots;
    std::function<QByteArray()> m_projectStateProvider;
    QList<PendingSidecarWrite> m_stateSidecarWrites;
    QThreadPool m_stateSidecarPool;
    int m_maxPendingStateSidecars{2};
    QSet<QString> m_scheduledStateHashes;
    QSet<QString> m_scheduledCheckpointHashes;
    QSet<QString> m_failedStateHashes;
    QSet<QString> m_failedCheckpointHashes;
    QString m_lastProjectStateHash;
    QList<QJsonObject> m_pendingActions;
    mutable QHash<QString, QString> m_stableEntityIds;
    QHash<QString, QString> m_undoEntryIds;
    QHash<QString, QString> m_commitTransactions;
    QString m_transactionId;
    QString m_transactionBoundary;
    QString m_transactionLabel;
    QString m_undoEntryId;
    QString m_targetTransactionId;
    QString m_lastCompletedTransactionId;
    QString m_lastCompletedUndoEntryId;
    QElapsedTimer m_lastCompletedTransaction;
    bool m_sessionEnded{false};
    bool m_projectContextRecorded{false};
};
