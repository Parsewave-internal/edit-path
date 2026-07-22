/*
    SPDX-FileCopyrightText: 2017 Nicolas Carion
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "undohelper.hpp"
#include "videopath/videopathrecorder.hpp"
#ifdef CRASH_AUTO_TEST
#include "logger.hpp"
#endif
#include <QDebug>
#include <QTime>
#include <utility>
FunctionalUndoCommand::FunctionalUndoCommand(Fun undo, Fun redo, const QString &text, QUndoCommand *parent)
    : QUndoCommand(parent)
    , m_undo(std::move(undo))
    , m_redo(std::move(redo))
    , m_undone(false)
{
    setText(QStringLiteral("%1 %2").arg(QTime::currentTime().toString("hh:mm")).arg(text));
}

void FunctionalUndoCommand::undo()
{
#ifdef CRASH_AUTO_TEST
    Logger::log_undo(true);
#endif
    m_undone = true;
    VideoPathRecorder &recorder = VideoPathRecorder::instance();
    recorder.beginTransaction(QStringLiteral("undo"), text(), QString::number(reinterpret_cast<quintptr>(this), 16));
    bool res = m_undo();
    Q_ASSERT(res);
    recorder.recordHistory(QStringLiteral("undo"), text());
    recorder.captureTimelineChange(text(), QStringLiteral("undo"));
    recorder.endTransaction();
    QUndoCommand::undo();
}

void FunctionalUndoCommand::redo()
{
    if (m_undone) {
#ifdef CRASH_AUTO_TEST
        Logger::log_undo(false);
#endif
        VideoPathRecorder &recorder = VideoPathRecorder::instance();
        recorder.beginTransaction(QStringLiteral("redo"), text(), QString::number(reinterpret_cast<quintptr>(this), 16));
        bool res = m_redo();
        Q_ASSERT(res);
        recorder.recordHistory(QStringLiteral("redo"), text());
        recorder.captureTimelineChange(text(), QStringLiteral("redo"));
        recorder.endTransaction();
    }
    QUndoCommand::redo();
}
