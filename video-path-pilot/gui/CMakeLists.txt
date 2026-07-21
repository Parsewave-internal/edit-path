# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only

cmake_minimum_required(VERSION 3.20)
project(EditPathCollector VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Qt6 6.5 REQUIRED COMPONENTS Core Gui Widgets)

qt_add_executable(edit-path-collector main.cpp)
target_link_libraries(edit-path-collector PRIVATE Qt6::Core Qt6::Gui Qt6::Widgets)
install(TARGETS edit-path-collector RUNTIME DESTINATION bin)
