"""Migration notice for the retired desktop webcam attendance prototype.

The previous implementation used a committed QR allow-list and local log file.
That access mechanism has been removed. Use the authenticated browser app in
app.py; attendee QR tokens are not coordinator credentials.
"""


def main():
    print("The desktop webcam check-in flow is retired.")
    print("Run the authenticated web app with: python app.py")


if __name__ == "__main__":
    main()
