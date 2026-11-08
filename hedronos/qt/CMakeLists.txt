# HedronOS GUI host — the QML shells in hedronos-store / hedronos-firstsetup need exactly
# this one type. SPDX-License-Identifier: MIT
cmake_minimum_required(VERSION 3.22)
project(hedron-gui LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_AUTOMOC ON)
find_package(Qt6 6.5 REQUIRED COMPONENTS Core Qml Quick Gui)
# Kirigami is optional on purpose: without it the QML still loads in a plain qml6 viewer with
# the `import org.kde.kirigami` lines satisfied by the bundled compatibility shim.
find_package(KF6Kirigami CONFIG QUIET)
find_package(KF6I18n CONFIG QUIET)

qt_add_executable(hedron-gui
    hedronfeed.cpp
    hedronfeed.h
    hedronstore_main.cpp
)
target_link_libraries(hedron-gui PRIVATE Qt6::Core Qt6::Qml Qt6::Quick Qt6::Gui)
if(KF6Kirigami_FOUND)
    target_link_libraries(hedron-gui PRIVATE KF6::Kirigami2)
    target_compile_definitions(hedron-gui PRIVATE HEDRON_HAVE_KIRIGAMI=1)
endif()
if(KF6I18n_FOUND)
    target_link_libraries(hedron-gui PRIVATE KF6::I18n)
endif()

install(TARGETS hedron-gui RUNTIME DESTINATION bin)
