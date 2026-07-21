/*
    SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
    SPDX-License-Identifier: GPL-3.0-only
*/

#include "videopathrecorder.hpp"

#include <QAction>
#include <QApplication>
#include <QCoreApplication>
#include <QDateTime>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QKeySequence>
#include <QMenu>
#include <QMouseEvent>
#include <QMutexLocker>
#include <QShortcutEvent>
#include <QSysInfo>
#include <QTimer>
#include <QUuid>
#include <QWidget>

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
    QTimer::singleShot(0, this, &VideoPathRecorder::attachActions);
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
    if (m_lastShortcut.isValid() && m_lastShortcut.elapsed() < 500) {
        source = QStringLiteral("keyboard");
    } else if (m_lastMenuClick.isValid() && m_lastMenuClick.elapsed() < 500) {
        source = QStringLiteral("menu");
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
    event.insert(QStringLiteral("interaction_id"), QUuid::createUuid().toString(QUuid::WithoutBraces));
    event.insert(QStringLiteral("command_id"), commandId);
    event.insert(QStringLiteral("label"), label);
    event.insert(QStringLiteral("source"), source);
    event.insert(QStringLiteral("checked"), checked);
    event.insert(QStringLiteral("shortcuts"), shortcuts);
    event.insert(QStringLiteral("focus"), describeObject(QApplication::focusWidget()));
    writeEvent(event);
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

bool VideoPathRecorder::belongsToTimeline(const QObject *object)
{
    for (const QObject *current = object; current; current = current->parent()) {
        if (describeObject(current).contains(QStringLiteral("timeline"), Qt::CaseInsensitive)) {
            return true;
        }
    }
    return false;
}

bool VideoPathRecorder::eventFilter(QObject *watched, QEvent *event)
{
    if (event->type() == QEvent::ChildAdded || event->type() == QEvent::Show) {
        QTimer::singleShot(0, this, &VideoPathRecorder::attachActions);
    } else if (event->type() == QEvent::Shortcut) {
        const auto *shortcut = static_cast<QShortcutEvent *>(event);
        m_lastShortcut.restart();
        QJsonObject shortcutEvent;
        shortcutEvent.insert(QStringLiteral("event_type"), QStringLiteral("ui.shortcut"));
        shortcutEvent.insert(QStringLiteral("interaction_id"), QUuid::createUuid().toString(QUuid::WithoutBraces));
        shortcutEvent.insert(QStringLiteral("key_sequence"), shortcut->key().toString(QKeySequence::PortableText));
        shortcutEvent.insert(QStringLiteral("ambiguous"), shortcut->isAmbiguous());
        shortcutEvent.insert(QStringLiteral("focus"), describeObject(QApplication::focusWidget()));
        writeEvent(shortcutEvent);
    } else if (event->type() == QEvent::MouseButtonRelease && qobject_cast<QMenu *>(watched)) {
        m_lastMenuClick.restart();
    }

    if (belongsToTimeline(watched) && (event->type() == QEvent::MouseButtonPress || event->type() == QEvent::MouseButtonRelease)) {
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
