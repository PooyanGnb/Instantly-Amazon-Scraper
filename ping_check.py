import platform
import subprocess
import sys


def main() -> int:
    host = "8.8.8.8"
    count_flag = "-n" if platform.system().lower() == "windows" else "-c"
    cmd = ["ping", count_flag, "10", host]

    try:
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        ) as proc:
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="")
            return proc.wait()
    except FileNotFoundError:
        print("ping command not found on this system.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
