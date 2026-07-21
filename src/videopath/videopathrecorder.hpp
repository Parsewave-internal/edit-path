/*
    SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
    SPDX-License-Identifier: GPL-3.0-only
*/

#pragma once

#include <QElapsedTimer>
#include <QJsonObject>
#include <QMutex>
#include <QObject>
#include <QPointF>
#include <QSet>
#include <QString>

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
    void recordAction(const QString &action, const QString &timelineId, const QJsonObject &parameters);
    void recordHistory(const QString &operation, const QString &label);

private:
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
    bool eventFilter(QObject *watched, QEvent *event) override;
    static QString describeObject(const QObject *object);
    static bool isTimelineCanvasTarget(const QObject *object);
    static bool hasMenuAncestor(const QObject *object);
    static bool hasToolButtonAncestor(const QObject *object);

    mutable QMutex m_mutex;
    std::unique_ptr<QFile> m_file;
    QString m_sessionId;
    qint64 m_sequence{0};
    QSet<QAction *> m_actions;
    bool m_actionDiscoveryScheduled{false};
    QElapsedTimer m_lastShortcut;
    QElapsedTimer m_lastMenuClick;
    QElapsedTimer m_lastToolbarClick;
    QString m_lastInputInteractionId;
    QString m_lastShortcutSequence;
    QString m_pointerInteractionId;
    QPointF m_pointerStart;
    QString m_pointerTarget;
    int m_pointerButton{0};
};
