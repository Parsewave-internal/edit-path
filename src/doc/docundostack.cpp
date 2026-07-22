/*
    SPDX-FileCopyrightText: 2017 Nicolas Carion
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "docundostack.hpp"
#include "videopath/videopathrecorder.hpp"
#include <QUndoCommand>
#include <QUndoGroup>

namespace {
QString videoPathUndoEntryKey(const QUndoCommand *command)
{
    return QString::number(reinterpret_cast<quintptr>(command), 16);
}
}

DocUndoStack::DocUndoStack(QUndoGroup *parent)
    : QUndoStack(parent)
{
}

void DocUndoStack::clear()
{
    VideoPathRecorder::instance().resetUndoEntries();
    QUndoStack::clear();
}

// TODO: custom undostack everywhere do that
void DocUndoStack::push(QUndoCommand *cmd)
{
    const QString commandLabel = cmd->text();
    if (index() < count()) {
        VideoPathRecorder &recorder = VideoPathRecorder::instance();
        for (int commandIndex = index(); commandIndex < count(); ++commandIndex) {
            recorder.forgetUndoEntry(videoPathUndoEntryKey(command(commandIndex)));
        }
        Q_EMIT invalidate(index());
    }
    VideoPathRecorder &recorder = VideoPathRecorder::instance();
    recorder.beginTransaction(QStringLiteral("commit"), commandLabel, QString());
    QUndoStack::push(cmd);
    // QUndoStack may merge and delete the incoming command. Bind to the
    // command actually retained by the stack, and never dereference cmd after
    // push().
    const QUndoCommand *retained = index() > 0 ? command(index() - 1) : nullptr;
    recorder.bindTransactionUndoEntry(retained ? videoPathUndoEntryKey(retained) : QString());
    recorder.captureTimelineChange(commandLabel, QStringLiteral("commit"));
    recorder.endTransaction();
}
