import requests
import uuid
import time
import webbrowser


def get_plex_token():
    """
    An interactive script to retrieve a Plex access token by guiding the user
    through the PIN-based authentication flow.
    """
    # --- Configuration ---
    client_identifier = str(uuid.uuid4())
    product_name = "Plex Telegram Bot Setup"

    base_headers = {
        "X-Plex-Product": product_name,
        "X-Plex-Version": "1.0",
        "X-Plex-Platform": "Python",
        "X-Plex-Client-Identifier": client_identifier,
        "accept": "application/json",
    }

    pins_url = "https://plex.tv/api/v2/pins"

    print("--- Plex Token Retrieval Script (PIN Auth Flow) ---")
    print("This script will guide you through authorizing it to get a token.")
    print("You will be asked to sign in using your web browser.")
    print("-" * 50)
    input("Press Enter to begin...")

    # 1. Get a temporary PIN from Plex
    print("\nSTEP 1: Requesting a PIN from Plex...")
    try:
        response = requests.post(
            pins_url, headers=base_headers, data={"strong": "true"}
        )
        response.raise_for_status()  # Raises an exception for bad status codes (4xx or 5xx)

        pin_data = response.json()
        pin_id = pin_data["id"]
        pin_code = pin_data["code"]

        print(">>> Success! Received PIN details from Plex.")
    except requests.exceptions.RequestException as e:
        print("\n--- ERROR ---")
        print(f"Could not connect to Plex to get a PIN: {e}")
        return

    # 2. Construct the authentication URL and instruct the user
    print("\nSTEP 2: Authorize the script in your browser.")

    auth_url = (
        f"https://app.plex.tv/auth#?context%5Bdevice%5D%5Bproduct%5D={product_name}"
        f"&clientID={client_identifier}"
        f"&code={pin_code}"
    )

    print("\nPlease open the following URL in your web browser to sign in:")
    print(f"\n    {auth_url}\n")
    print(
        "After you sign in and see the 'You're all set!' message, please return to this window."
    )

    try:
        webbrowser.open(auth_url)
        print(
            "Your default web browser has been opened. If it hasn't, please copy the URL manually."
        )
    except webbrowser.Error:
        print(
            "Could not automatically open a web browser. Please copy the URL above and paste it into your browser."
        )

    print("-" * 50)
    print("STEP 3: Waiting for you to complete authentication...")

    # 3. Poll the PIN ID to see if it has been authorized
    start_time = time.time()
    final_token = None

    # Poll for a maximum of 3 minutes
    while time.time() - start_time < 180:
        try:
            print("Checking authentication status...")
            check_url = f"{pins_url}/{pin_id}"
            response = requests.get(check_url, headers=base_headers)
            response.raise_for_status()

            auth_check_data = response.json()
            if auth_check_data.get("authToken"):
                final_token = auth_check_data["authToken"]
                print(">>> Success! Authentication token has been retrieved.")
                break

            # Wait 5 seconds before checking again
            time.sleep(5)

        except requests.exceptions.RequestException as e:
            print(f"\nAn error occurred while checking the token: {e}")
            print("Aborting.")
            return

    # 4. Final output
    print("-" * 50)
    if final_token:
        print("\n\n" + "=" * 60)
        print("=== SUCCESS! Your Plex Access Token has been retrieved. ===")
        print("=" * 60)
        print("\nYour token is:")
        print(f"\n{final_token}\n")
        print(
            "Copy this token and paste it into your bot_config.ini file for the 'plex_token' value."
        )
        print("=" * 60)
    else:
        print("\n\n--- AUTHENTICATION FAILED ---")
        print("The script timed out waiting for authentication.")
        print("Please run the script again to get a new link.")


# This block ensures the get_plex_token() function is called when the script is executed.
if __name__ == "__main__":
    get_plex_token()
