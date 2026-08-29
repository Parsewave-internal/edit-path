// SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
// SPDX-License-Identifier: GPL-3.0-only

#include <QAudioDevice>
#include <QAudioFormat>
#include <QAudioSource>
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QMediaDevices>
#include <QMetaObject>
#include <QCommandLineParser>
#include <QIODevice>
#include <QTimer>
#include <QThread>

#include <cstdint>
#include <iostream>
#include <thread>
#include <utility>

namespace {

class WavWriter final : public QIODevice
{
public:
    WavWriter(QString path, QAudioFormat format, QObject *parent = nullptr)
        : QIODevice(parent)
        , m_path(std::move(path))
        , m_format(std::move(format))
    {
    }

    bool begin()
    {
        if (!m_file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            return false;
        }
        QByteArray header(44, '\0');
        if (m_file.write(header) != header.size()) {
            m_file.close();
            return false;
        }
        return open(QIODevice::WriteOnly);
    }

    bool finish()
    {
        if (!isOpen()) {
            return false;
        }
        close();
        if (!writeHeader()) {
            m_file.close();
            return false;
        }
        m_file.flush();
        m_file.close();
        return QFileInfo::exists(m_path) && QFileInfo(m_path).size() > 44;
    }

protected:
    qint64 writeData(const char *data, qint64 length) override
    {
        const qint64 written = m_file.write(data, length);
        if (written > 0) {
            m_dataBytes += written;
            // Keep the RIFF sizes valid even if the helper is terminated by
            // the supervisor after a device or process failure.
            writeHeader();
        }
        return written;
    }

    qint64 readData(char *, qint64) override { return -1; }

private:
    static void put32(QByteArray &bytes, int offset, quint32 value)
    {
        bytes[offset] = char(value & 0xff);
        bytes[offset + 1] = char((value >> 8) & 0xff);
        bytes[offset + 2] = char((value >> 16) & 0xff);
        bytes[offset + 3] = char((value >> 24) & 0xff);
    }

    static void put16(QByteArray &bytes, int offset, quint16 value)
    {
        bytes[offset] = char(value & 0xff);
        bytes[offset + 1] = char((value >> 8) & 0xff);
    }

    bool writeHeader()
    {
        if (!m_file.isOpen()) {
            return false;
        }
        const int channels = m_format.channelCount();
        const int sampleRate = m_format.sampleRate();
        const int sampleBytes = m_format.bytesPerSample();
        if (channels <= 0 || sampleRate <= 0 || sampleBytes <= 0) {
            return false;
        }
        const quint32 byteRate = quint32(sampleRate * channels * sampleBytes);
        const quint16 blockAlign = quint16(channels * sampleBytes);
        QByteArray header(44, '\0');
        header.replace(0, 4, "RIFF");
        put32(header, 4, quint32(36 + m_dataBytes));
        header.replace(8, 4, "WAVE");
        header.replace(12, 4, "fmt ");
        put32(header, 16, 16);
        // QAudioSource normally uses Int16, but some Windows devices only
        // expose Float32 or another native format. Preserve that format in
        // the RIFF header so Whisper/Qt decode the samples correctly.
        const quint16 wavFormat = m_format.sampleFormat() == QAudioFormat::Float ? 3 : 1;
        put16(header, 20, wavFormat);
        put16(header, 22, quint16(channels));
        put32(header, 24, quint32(sampleRate));
        put32(header, 28, byteRate);
        put16(header, 32, blockAlign);
        put16(header, 34, quint16(sampleBytes * 8));
        header.replace(36, 4, "data");
        put32(header, 40, quint32(m_dataBytes));
        if (!m_file.seek(0) || m_file.write(header) != header.size() || !m_file.seek(44 + m_dataBytes)) {
            return false;
        }
        return true;
    }

    QString m_path;
    QAudioFormat m_format;
    QFile m_file{m_path};
    qint64 m_dataBytes{0};
};

QAudioDevice findDevice(const QString &description)
{
    const auto inputs = QMediaDevices::audioInputs();
    for (const QAudioDevice &device : inputs) {
        if (device.description() == description || QString::fromUtf8(device.id()) == description) {
            return device;
        }
    }
    if (description.isEmpty() || description == QStringLiteral("default")) {
        return QMediaDevices::defaultAudioInput();
    }
    return {};
}

int run(const QString &deviceDescription, const QString &output, int durationSeconds)
{
    const QAudioDevice device = findDevice(deviceDescription);
    if (device.isNull()) {
        std::cerr << "microphone_not_found\n";
        return 2;
    }
    QAudioFormat format;
    format.setSampleRate(16000);
    format.setChannelCount(1);
    format.setSampleFormat(QAudioFormat::Int16);
    if (!device.isFormatSupported(format)) {
        format = device.preferredFormat();
    }
    if (format.sampleFormat() == QAudioFormat::Unknown || format.channelCount() <= 0 || format.sampleRate() <= 0) {
        std::cerr << "microphone_format_not_supported\n";
        return 3;
    }
    QDir().mkpath(QFileInfo(output).absolutePath());
    WavWriter writer(output, format);
    if (!writer.begin()) {
        std::cerr << "audio_output_open_failed\n";
        return 4;
    }
    QCoreApplication *application = QCoreApplication::instance();
    QAudioSource source(device, format);
    if (durationSeconds > 0) {
        QObject::connect(&source, &QAudioSource::stateChanged, application, [&source, application](QAudio::State state) {
            if (state == QAudio::StoppedState && source.error() != QAudio::NoError) {
                QMetaObject::invokeMethod(application, "quit", Qt::QueuedConnection);
            }
        });
    }
    source.setBufferSize(format.bytesForDuration(100000));
    source.start(&writer);
    if (source.error() != QAudio::NoError) {
        writer.finish();
        std::cerr << "microphone_start_failed\n";
        return 5;
    }
    std::cout << "recording_started\n" << std::flush;

    std::thread control;
    if (durationSeconds <= 0) {
        control = std::thread([&] {
            std::string command;
            while (std::getline(std::cin, command)) {
                if (command == "q" || command == "stop") {
                    QMetaObject::invokeMethod(application, "quit", Qt::QueuedConnection);
                    return;
                }
            }
            QMetaObject::invokeMethod(application, "quit", Qt::QueuedConnection);
        });
    }
    QTimer::singleShot(durationSeconds > 0 ? durationSeconds * 1000 : 0, application, [&] {
        if (durationSeconds > 0) {
            application->quit();
        }
    });
    const int result = application->exec();
    source.stop();
    const bool completed = writer.finish();
    if (control.joinable()) {
        control.join();
    }
    if (!completed) {
        std::cerr << "audio_output_finalize_failed\n";
        return 6;
    }
    std::cout << "recording_stopped\n" << std::flush;
    return result;
}

} // namespace

int main(int argc, char **argv)
{
    QCoreApplication application(argc, argv);
    QCommandLineParser options;
    options.setApplicationDescription(QStringLiteral("EditPath native microphone recorder"));
    options.addHelpOption();
    options.addOption({QStringLiteral("device"), QStringLiteral("microphone description or device id"), QStringLiteral("name")});
    options.addOption({QStringLiteral("output"), QStringLiteral("WAV output path"), QStringLiteral("path")});
    options.addOption({QStringLiteral("duration"), QStringLiteral("stop automatically after seconds (for microphone tests)"), QStringLiteral("seconds")});
    options.process(application);
    const QString output = options.value(QStringLiteral("output"));
    if (output.isEmpty()) {
        std::cerr << "missing_output\n";
        return 1;
    }
    bool validDuration = false;
    const int duration = options.value(QStringLiteral("duration")).toInt(&validDuration);
    return run(options.value(QStringLiteral("device")), output, validDuration ? qMax(0, duration) : 0);
}
