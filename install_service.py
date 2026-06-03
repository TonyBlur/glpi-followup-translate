#!/usr/bin/env python3
"""Install GLPI Followup Translate as a background service.

Detects the operating system and installs accordingly:
  Linux   → systemd service
  Windows → NSSM or Task Scheduler
  macOS   → launchd agent

Usage:
  python install_service.py               # auto-detect
  python install_service.py --remove      # uninstall
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import textwrap


SYSTEM = platform.system()
SERVICE_NAME = "glpi-translate"
WORK_DIR = os.getcwd()
PYTHON = sys.executable
CLI_CMD = f"{PYTHON} -m glpi_followup_translate"


def run(cmd, **kwargs):
    """Run a command, print it, and check exit code."""
    print(f"  $ {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
    subprocess.run(cmd, check=True, **kwargs)


def install_linux():
    """Install systemd service."""
    service_template = os.path.join(os.path.dirname(__file__), "deploy", "glpi-translate.service")
    if not os.path.exists(service_template):
        print("Error: deploy/glpi-translate.service not found.")
        print("Run from the project root directory.")
        sys.exit(1)

    service_content = textwrap.dedent(f"""\
        [Unit]
        Description=GLPI Followup Translate
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        WorkingDirectory={WORK_DIR}
        ExecStart={CLI_CMD}
        Restart=always
        RestartSec=10
        StandardOutput=append:{WORK_DIR}/glpi-translate.log
        StandardError=append:{WORK_DIR}/glpi-translate.log

        [Install]
        WantedBy=multi-user.target
    """)

    unit_path = f"/etc/systemd/system/{SERVICE_NAME}.service"
    if os.geteuid() != 0:
        print("Error: needs root. Run: sudo python install_service.py")
        sys.exit(1)

    with open(unit_path, "w") as f:
        f.write(service_content)
    print(f"  Created {unit_path}")

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", SERVICE_NAME])
    print(f"\nService installed. Check: sudo systemctl status {SERVICE_NAME}")
    print(f"  Logs:    sudo journalctl -u {SERVICE_NAME} -f")


def remove_linux():
    if os.geteuid() != 0:
        print("Error: needs root. Run: sudo python install_service.py --remove")
        sys.exit(1)
    run(["systemctl", "stop", SERVICE_NAME])
    run(["systemctl", "disable", SERVICE_NAME])
    os.remove(f"/etc/systemd/system/{SERVICE_NAME}.service")
    run(["systemctl", "daemon-reload"])
    print("Service removed.")


def install_windows():
    """Install via Windows Task Scheduler (no extra tools needed)."""
    xml_path = os.path.join(WORK_DIR, "deploy", "glpi-translate-task.xml")

    xml = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task" version="1.4">
          <RegistrationInfo>
            <Description>GLPI Followup Translate daemon</Description>
          </RegistrationInfo>
          <Triggers>
            <BootTrigger>
              <Enabled>true</Enabled>
            </BootTrigger>
          </Triggers>
          <Principals>
            <Principal id="Author">
              <RunLevel>LeastPrivilege</RunLevel>
            </Principal>
          </Principals>
          <Settings>
            <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
            <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
            <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
            <RestartOnFailure>
              <Interval>PT1M</Interval>
              <Count>999</Count>
            </RestartOnFailure>
          </Settings>
          <Actions Context="Author">
            <Exec>
              <Command>{PYTHON}</Command>
              <Arguments>-m glpi_followup_translate</Arguments>
              <WorkingDirectory>{WORK_DIR}</WorkingDirectory>
            </Exec>
          </Actions>
        </Task>
    """)

    os.makedirs(os.path.dirname(xml_path), exist_ok=True)
    with open(xml_path, "w") as f:
        f.write(xml)

    try:
        subprocess.run(
            ["schtasks", "/Create", "/TN", SERVICE_NAME, "/XML", xml_path, "/F"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(["schtasks", "/Run", "/TN", SERVICE_NAME], check=True)
        print(f"\nTask '{SERVICE_NAME}' installed and started.")
        print(f"  Check:  schtasks /Query /TN {SERVICE_NAME}")
        print(f"  Remove: python install_service.py --remove")
    except subprocess.CalledProcessError as e:
        print(f"Error: {e.stderr}")
        print("\nTry running as Administrator.")
        sys.exit(1)


def remove_windows():
    subprocess.run(["schtasks", "/Delete", "/TN", SERVICE_NAME, "/F"], check=True)
    print("Task removed.")


def install_macos():
    """Install launchd agent."""
    plist_content = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>com.glpi.translate</string>
            <key>ProgramArguments</key>
            <array>
                <string>{PYTHON}</string>
                <string>-m</string>
                <string>glpi_followup_translate</string>
            </array>
            <key>WorkingDirectory</key>
            <string>{WORK_DIR}</string>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{WORK_DIR}/glpi-translate.log</string>
            <key>StandardErrorPath</key>
            <string>{WORK_DIR}/glpi-translate.log</string>
        </dict>
        </plist>
    """)

    plist_path = os.path.expanduser(f"~/Library/LaunchAgents/com.glpi.translate.plist")
    os.makedirs(os.path.dirname(plist_path), exist_ok=True)
    with open(plist_path, "w") as f:
        f.write(plist_content)
    print(f"  Created {plist_path}")

    run(["launchctl", "load", plist_path])
    print(f"\nAgent installed. Check: launchctl list | grep glpi")
    print(f"  Remove: python install_service.py --remove")


def remove_macos():
    plist_path = os.path.expanduser(f"~/Library/LaunchAgents/com.glpi.translate.plist")
    run(["launchctl", "unload", plist_path])
    os.remove(plist_path)
    print("Agent removed.")


def main():
    parser = argparse.ArgumentParser(description="Install GLPI Translate as background service")
    parser.add_argument("--remove", action="store_true", help="Uninstall the service")
    args = parser.parse_args()

    action = "Removing" if args.remove else "Installing"
    print(f"{action} background service on {SYSTEM}...\n")

    if SYSTEM == "Linux":
        if args.remove:
            remove_linux()
        else:
            install_linux()
    elif SYSTEM == "Windows":
        if args.remove:
            remove_windows()
        else:
            install_windows()
    elif SYSTEM == "Darwin":
        if args.remove:
            remove_macos()
        else:
            install_macos()
    else:
        print(f"Unsupported OS: {SYSTEM}")
        sys.exit(1)


if __name__ == "__main__":
    main()
