#include <sys/resource.h>

#include <csignal>
#include <ctime>
#include <execinfo.h>
#include <fcntl.h>
#include <ucontext.h>
#include <unistd.h>

#include <QApplication>
#include <QTranslator>

#include "system/hardware/hw.h"
#include "selfdrive/ui/qt/qt_window.h"
#include "selfdrive/ui/qt/util.h"
#include "selfdrive/ui/qt/window.h"

namespace {

constexpr const char *kCrashLogPath = "/data/log/ui_crash.log";

// Signal-safe RSS read from /proc/self/status. Returns MB, or -1 on failure.
int signal_safe_rss_mb() {
  char buf[2048];
  int fd = open("/proc/self/status", O_RDONLY | O_CLOEXEC);
  if (fd < 0) return -1;
  ssize_t n = read(fd, buf, sizeof(buf) - 1);
  close(fd);
  if (n <= 0) return -1;
  buf[n] = '\0';
  for (const char *p = buf; *p;) {
    if (p[0] == 'V' && p[1] == 'm' && p[2] == 'R' && p[3] == 'S' && p[4] == 'S' && p[5] == ':') {
      p += 6;
      while (*p == ' ' || *p == '\t') p++;
      int kb = 0;
      while (*p >= '0' && *p <= '9') { kb = kb * 10 + (*p - '0'); p++; }
      return kb / 1024;
    }
    while (*p && *p != '\n') p++;
    if (*p == '\n') p++;
  }
  return -1;
}

// Signal-safe formatted write of the crash header to fd.
void write_crash_header(int fd, int sig, void *fault_addr) {
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  struct tm tm;
  localtime_r(&ts.tv_sec, &tm);

  const char *sig_name = "UNKNOWN";
  switch (sig) {
    case SIGSEGV: sig_name = "SIGSEGV"; break;
    case SIGABRT: sig_name = "SIGABRT"; break;
    case SIGBUS:  sig_name = "SIGBUS";  break;
    case SIGFPE:  sig_name = "SIGFPE";  break;
    case SIGILL:  sig_name = "SIGILL";  break;
  }

  char line[512];
  int n = snprintf(line, sizeof(line),
    "[%04d-%02d-%02d %02d:%02d:%02d] UI CRASH sig=%s pid=%d addr=%p rss=%dMB\n",
    tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
    tm.tm_hour, tm.tm_min, tm.tm_sec,
    sig_name, getpid(), fault_addr, signal_safe_rss_mb());
  if (n > 0) {
    if (fd >= 0) (void)!write(fd, line, n);
    (void)!write(STDERR_FILENO, line, n);
  }
}

// Signal-safe append of /proc/self/task/<tid>/stack to fd.
void write_kernel_stack(int fd) {
  if (fd < 0) return;
  const char hdr[] = "--- kernel_stack ---\n";
  (void)!write(fd, hdr, sizeof(hdr) - 1);
  int sfd = open("/proc/self/stack", O_RDONLY | O_CLOEXEC);
  if (sfd >= 0) {
    char buf[4096];
    ssize_t n;
    while ((n = read(sfd, buf, sizeof(buf))) > 0) {
      (void)!write(fd, buf, n);
    }
    close(sfd);
  }
  const char end[] = "--- end ---\n";
  (void)!write(fd, end, sizeof(end) - 1);
}

// Dump aarch64 registers from ucontext. PC + LR alone are enough to locate the
// crash with addr2line, even if backtrace_symbols_fd fails.
void write_registers(int fd, void *ucontext) {
  if (fd < 0 || ucontext == nullptr) return;
  const char hdr[] = "--- registers ---\n";
  (void)!write(fd, hdr, sizeof(hdr) - 1);
#if defined(__aarch64__)
  ucontext_t *uc = static_cast<ucontext_t *>(ucontext);
  char line[256];
  int n = snprintf(line, sizeof(line),
    "pc=0x%llx lr=0x%llx sp=0x%llx fp=0x%llx fault=0x%llx\n",
    (unsigned long long)uc->uc_mcontext.pc,
    (unsigned long long)uc->uc_mcontext.regs[30],
    (unsigned long long)uc->uc_mcontext.sp,
    (unsigned long long)uc->uc_mcontext.regs[29],
    (unsigned long long)uc->uc_mcontext.fault_address);
  if (n > 0) (void)!write(fd, line, n);
#else
  const char unsupp[] = "<not aarch64>\n";
  (void)!write(fd, unsupp, sizeof(unsupp) - 1);
#endif
}

// Userspace backtrace. backtrace_symbols_fd can call malloc internally which
// is not strictly async-signal-safe, but on glibc/musl it works for SIGSEGV
// crashes that leave the heap intact (our case: null deref).
void write_user_backtrace(int fd) {
  if (fd < 0) return;
  const char hdr[] = "--- backtrace ---\n";
  (void)!write(fd, hdr, sizeof(hdr) - 1);
  void *frames[32];
  int n = backtrace(frames, 32);
  if (n > 0) {
    backtrace_symbols_fd(frames, n, fd);
  }
}

void crash_handler(int sig, siginfo_t *info, void *ucontext) {
  int fd = open(kCrashLogPath, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
  void *fault_addr = info ? info->si_addr : nullptr;
  write_crash_header(fd, sig, fault_addr);
  write_registers(fd, ucontext);
  write_user_backtrace(fd);
  write_kernel_stack(fd);
  if (fd >= 0) close(fd);

  // Re-raise with the default handler so the process actually dies and the
  // manager sees the normal exit status. Manager will restart us.
  struct sigaction sa{};
  sa.sa_handler = SIG_DFL;
  sigaction(sig, &sa, nullptr);
  raise(sig);
}

void install_crash_handlers() {
  struct sigaction sa{};
  sa.sa_flags = SA_SIGINFO | SA_RESETHAND;
  sa.sa_sigaction = crash_handler;
  sigemptyset(&sa.sa_mask);
  for (int sig : {SIGSEGV, SIGABRT, SIGBUS, SIGFPE, SIGILL}) {
    sigaction(sig, &sa, nullptr);
  }
}

}  // namespace

int main(int argc, char *argv[]) {
  setpriority(PRIO_PROCESS, 0, -20);

  install_crash_handlers();

  qInstallMessageHandler(swagLogMessageHandler);
  initApp(argc, argv);

  QTranslator translator;
  QString translation_file = QString::fromStdString(Params().get("LanguageSetting"));
  if (!translator.load(QString(":/%1").arg(translation_file)) && translation_file.length()) {
    qCritical() << "Failed to load translation file:" << translation_file;
  }

  QApplication a(argc, argv);
  a.installTranslator(&translator);

  MainWindow w;
  setMainWindow(&w);
  a.installEventFilter(&w);
  return a.exec();
}
