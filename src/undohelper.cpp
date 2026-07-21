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
    bool res = m_undo();
    Q_ASSERT(res);
    VideoPathRecorder::instance().recordHistory(QStringLiteral("undo"), text());
    VideoPathRecorder::instance().captureTimelineChange(text(), QStringLiteral("undo"));
    QUndoCommand::undo();
}

void FunctionalUndoCommand::redo()
{
    if (m_undone) {
#ifdef CRASH_AUTO_TEST
        Logger::log_undo(false);
#endif
        bool res = m_redo();
        Q_ASSERT(res);
        VideoPathRecorder::instance().recordHistory(QStringLiteral("redo"), text());
        VideoPathRecorder::instance().captureTimelineChange(text(), QStringLiteral("redo"));
    }
    QUndoCommand::redo();
}
