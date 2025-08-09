# file: restart_agent.py
# (Run this script on your Plex Media Server machine)

from flask import Flask, request, jsonify
import os
import platform
import subprocess

# --- CONFIGURATION ---
# IMPORTANT: Choose a long, random string for your secret key.
# This is the ONLY password the bot needs to send.
SECRET_KEY = "your_very_secret_and_long_random_string"
PORT = 9988 # The port the agent will listen on.

app = Flask(__name__)

@app.route('/restart-plex', methods=['POST'])
def restart_plex_endpoint():
    # 1. Authenticate the request
    auth_header = request.headers.get('X-Auth-Token')
    if not auth_header or auth_header != SECRET_KEY:
        print("[AGENT] Received unauthorized request. Ignoring.")
        return jsonify({"error": "Unauthorized"}), 401

    print("[AGENT] Authorized request received. Attempting to restart Plex...")
    command_to_run = []
    system = platform.system()

    # 2. Determine the correct command for the OS
    if system == "Linux":
        # Assumes systemd is used. Adjust if using 'service' or other init systems.
        command_to_run = ["sudo", "systemctl", "restart", "plexmediaserver.service"]
    elif system == "Windows":
        command_to_run = ["powershell", "-Command", "Restart-Service -Name 'Plex Media Server'"]
    elif system == "Darwin": # macOS
        command_to_run = ["launchctl", "stop", "com.plexapp.plexmediaserver", "&&", "launchctl", "start", "com.plexapp.plexmediaserver"]
    else:
        print(f"[AGENT] Unsupported operating system: {system}")
        return jsonify({"error": f"Unsupported OS: {system}"}), 500

    # 3. Execute the command
    try:
        print(f"[AGENT] Running command: {' '.join(command_to_run)}")
        subprocess.run(command_to_run, check=True, capture_output=True, text=True)
        print("[AGENT] Plex restart command executed successfully.")
        return jsonify({"status": "success", "message": "Plex restart command sent."}), 200
    except subprocess.CalledProcessError as e:
        error_output = e.stderr or e.stdout
        print(f"[AGENT] Error executing command: {error_output}")
        return jsonify({"status": "error", "message": f"Command failed: {error_output}"}), 500
    except FileNotFoundError:
        print("[AGENT] Error: 'sudo' or the command itself not found. Ensure the command is correct and the agent has the right permissions.")
        return jsonify({"status": "error", "message": "'sudo' or 'powershell' not found. Check agent permissions."}), 500

if __name__ == '__main__':
    print(f"--- Plex Restart Agent ---")
    print(f"Listening for requests on port {PORT}")
    print("IMPORTANT: Ensure you have configured any firewalls to allow incoming traffic on this port.")
    # Use 0.0.0.0 to listen on all available network interfaces
    app.run(host='0.0.0.0', port=PORT)