#include <chrono>
#include <thread>

#ifdef _WIN32
#include <windows.h>
#else
#include <sys/types.h>
#include <unistd.h>
#endif

int main() {
#ifdef _WIN32
    STARTUPINFOA startup{};
    PROCESS_INFORMATION process{};
    startup.cb = sizeof(startup);
    char command[] = "cmd.exe /c ping 127.0.0.1 -n 10 >NUL";
    if (!CreateProcessA(nullptr, command, nullptr, nullptr, FALSE, 0, nullptr, nullptr, &startup, &process))
        return 77;
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
#else
    pid_t child = fork();
    if (child < 0)
        return 77;
    if (child == 0) {
        std::this_thread::sleep_for(std::chrono::seconds(30));
        _exit(0);
    }
#endif
    std::this_thread::sleep_for(std::chrono::seconds(30));
}
