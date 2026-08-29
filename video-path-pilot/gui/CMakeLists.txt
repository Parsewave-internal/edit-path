# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only

cmake_minimum_required(VERSION 3.20)
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    project(EditPathRecorder VERSION 0.1.0 LANGUAGES CXX)
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# The recorder uses only Qt 6.4 APIs. Keeping the minimum aligned with Ubuntu
# 24.04 also lets CI compile the same GUI source used by Windows packaging.
find_package(Qt6 6.4 REQUIRED COMPONENTS Core Gui Widgets Multimedia)

qt_add_executable(edit-path-recorder main.cpp)
set_target_properties(edit-path-recorder PROPERTIES OUTPUT_NAME EditPath)
target_link_libraries(edit-path-recorder PRIVATE Qt6::Core Qt6::Gui Qt6::Widgets Qt6::Multimedia)
install(TARGETS edit-path-recorder RUNTIME DESTINATION bin)

qt_add_executable(edit-path-audio audio-helper.cpp)
set_target_properties(edit-path-audio PROPERTIES OUTPUT_NAME EditPathAudio)
target_link_libraries(edit-path-audio PRIVATE Qt6::Core Qt6::Multimedia)
install(TARGETS edit-path-audio RUNTIME DESTINATION bin)
