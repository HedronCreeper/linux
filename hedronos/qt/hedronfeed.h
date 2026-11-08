// HedronFeed — the one QML↔CLI bridge both GUI shells use. SPDX-License-Identifier: MIT
//
// The store and the installer GUI are deliberately *dumb*: they shell out to `hstore`/
// `hedron-setup` and render JSON. That keeps the GUI and the terminal from ever disagreeing,
// and it means the GUI can be replaced (or audited) without touching any install semantics.
// This file is the whole privileged surface: run a command, get stdout back.
#ifndef HEDRONFEED_H
#define HEDRONFEED_H

#include <QObject>
#include <QQmlListProperty>
#include <QStringList>
#include <QVariantList>

class QProcess;

class HedronFeed : public QObject {
    Q_OBJECT
    Q_PROPERTY(bool busy READ busy NOTIFY busyChanged)
    Q_PROPERTY(QString lastError READ lastError NOTIFY resultReady)

public:
    explicit HedronFeed(QObject *parent = nullptr);
    ~HedronFeed() override;

    bool busy() const { return m_busy; }
    QString lastError() const { return m_lastError; }

    /// Run argv (no shell), parse stdout as JSON when it parses, hand the result to QML.
    /// @param argv  program + arguments
    /// @param timeoutMs  hard kill after this; 0 means "no timeout"
    Q_INVOKABLE void run(const QVariantList &argv, int timeoutMs = 120000);

    /// Read a workspace file (selection JSON, feature doc) — used for the installer screens.
    Q_INVOKABLE QString readText(const QString &path);

signals:
    void busyChanged();
    /// reply is the parsed JSON object (or a string when it is not JSON), ok is the exit status.
    void resultReady(const QString &requestId, bool ok, const QString &reply);

private slots:
    void onFinished(int exitCode, int exitStatus);

private:
    QProcess *m_process = nullptr;
    QByteArray m_buffer;
    QString m_pendingId;
    bool m_busy = false;
    QString m_lastError;
};

#endif // HEDRONFEED_H
