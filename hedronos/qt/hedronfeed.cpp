// SPDX-License-Identifier: MIT
#include "hedronfeed.h"

#include <QFile>
#include <QJsonDocument>
#include <QProcess>
#include <QTimer>

HedronFeed::HedronFeed(QObject *parent)
    : QObject(parent)
{
}

HedronFeed::~HedronFeed()
{
    if (m_process && m_process->state() != QProcess::NotRunning) {
        m_process->kill();
        m_process->waitForFinished(1000);
    }
}

void HedronFeed::run(const QVariantList &argv, int timeoutMs)
{
    if (m_busy) {
        emit resultReady(QString(), false, QStringLiteral("another command is still running"));
        return;
    }
    if (argv.isEmpty()) {
        emit resultReady(QString(), false, QStringLiteral("empty command"));
        return;
    }

    QStringList args;
    for (int i = 1; i < argv.size(); ++i) {
        args << argv.at(i).toString();
    }

    m_process = new QProcess(this);
    m_process->setProcessChannelMode(QProcess::MergedChannels);
    m_process->setProgram(argv.at(0).toString());
    m_process->setArguments(args);
    connect(m_process, &QProcess::readyReadStandardOutput, this, [this]() {
        m_buffer += m_process->readAllStandardOutput();
    });
    connect(m_process, &QProcess::finished, this, &HedronFeed::onFinished);
    connect(m_process, &QProcess::errorOccurred, this, [this](QProcess::ProcessError) {
        if (!m_process || m_process->exitStatus() != QProcess::CrashExit) {
            // The GUI must survive a store that is not installed yet: report, never crash.
            m_lastError = m_process ? m_process->errorString() : QStringLiteral("no process");
        }
    });

    if (timeoutMs > 0) {
        QTimer::singleShot(timeoutMs, m_process, [this]() {
            if (m_process && m_process->state() != QProcess::NotRunning) {
                m_lastError = QStringLiteral("timed out");
                m_process->kill();
            }
        });
    }

    m_buffer.clear();
    m_busy = true;
    emit busyChanged();
    m_process->start();
    if (!m_process->waitForStarted(5000)) {
        onFinished(-1, static_cast<int>(QProcess::CrashExit));
    }
}

QString HedronFeed::readText(const QString &path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return QString();
    }
    return QString::fromUtf8(file.readAll());
}

void HedronFeed::onFinished(int exitCode, int exitStatus)
{
    if (!m_process) {
        return;
    }
    const bool ok = (exitStatus == static_cast<int>(QProcess::NormalExit) && exitCode == 0);
    QString reply = QString::fromUtf8(m_buffer).trimmed();
    m_lastError.clear();
    if (!ok && reply.isEmpty()) {
        reply = m_process->errorString();
    }
    if (!reply.isEmpty()) {
        QJsonParseError err{};
        const QJsonDocument doc = QJsonDocument::fromJson(reply.toUtf8(), &err);
        if (err.error != QJsonParseError::NoError) {
            // Non-JSON output (a pacman progress bar, an error) is passed through verbatim so
            // the GUI can show it instead of swallowing the only useful message.
            emit resultReady(m_pendingId, ok, reply);
            m_process->deleteLater();
            m_process = nullptr;
            m_busy = false;
            emit busyChanged();
            return;
        }
    }
    emit resultReady(m_pendingId, ok, reply);
    m_process->deleteLater();
    m_process = nullptr;
    m_busy = false;
    emit busyChanged();
}
