/*
    SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
    SPDX-License-Identifier: GPL-3.0-only
*/

#include "videopathrecorder.hpp"

#include <QCoreApplication>
#include <QDateTime>
#include <QFile>
#include <QJsonDocument>
#include <QMutexLocker>
#include <QSysInfo>
#include <QUuid>

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
    writeEvent(event);
}

void VideoPathRecorder::writeEvent(QJsonObject event)
{
    QMutexLocker locker(&m_mutex);
    if (!m_file || !m_file->isOpen()) {
        return;
    }
    event.insert(QStringLiteral("schema_version"), QStringLiteral("0.1.0"));
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
    event.insert(QStringLiteral("schema_version"), QStringLiteral("0.1.0"));
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
