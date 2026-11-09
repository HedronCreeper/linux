// HedronOS Store / Setup GUI host. SPDX-License-Identifier: MIT
//
// One tiny QApplication that registers HedronFeed and loads whichever .qml it is pointed at,
// so both GUI shells share the same binary and the same sandbox.
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QUrl>

#include "hedronfeed.h"

int main(int argc, char *argv[])
{
    QGuiApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("HedronOS"));
    app.setOrganizationName(QStringLiteral("HedronOS"));

    qmlRegisterType<HedronFeed>("local.hedron.feed", 1, 0, "HedronFeed");

    QQmlApplicationEngine engine;
    const QStringList args = app.arguments();
    const QString source = args.isEmpty() ? QStringLiteral("/usr/share/hedron/store/Store.qml") : args.first();
    engine.load(QUrl::fromLocalFile(source));
    if (engine.rootObjects().isEmpty()) {
        return 1;   // the caller falls back to the TUI on this exit code
    }
    return app.exec();
}
