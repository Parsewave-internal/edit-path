/*
    SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
    SPDX-License-Identifier: GPL-3.0-only
*/

#pragma once

#include <QJsonObject>
#include <QMutex>
#include <QString>

#include <memory>

class QFile;

/** Append-only recorder for software-independent Video Path events. */
class VideoPathRecorder
{
public:
    static VideoPathRecorder &instance();

    bool isEnabled() const;
    void recordAction(const QString &action, const QString &timelineId, const QJsonObject &parameters);
    void recordHistory(const QString &operation, const QString &label);

private:
    VideoPathRecorder();
    ~VideoPathRecorder();
    Q_DISABLE_COPY_MOVE(VideoPathRecorder)

    void writeEvent(QJsonObject event);
    void writeSessionStart();

    mutable QMutex m_mutex;
    std::unique_ptr<QFile> m_file;
    QString m_sessionId;
    qint64 m_sequence{0};
};
