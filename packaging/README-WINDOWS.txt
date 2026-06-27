Media Browser for Windows
=========================

If Windows says "Windows protected your PC", "Unknown publisher", or
"This app might be unsafe", the package is being blocked by Microsoft
SmartScreen because it was downloaded from the Internet and is not signed
with a trusted Windows code-signing certificate.

Recommended first-run steps:

1. Right-click the downloaded ZIP file before extracting it.
2. Open Properties.
3. Check "Unblock" if it is shown, then click OK.
4. Extract the ZIP again.
5. Run MediaBrowser.exe.

PowerShell alternative:

    Unblock-File .\MediaBrowser-vVERSION-windows.zip

Then extract the ZIP and run MediaBrowser.exe.

For official publisher verification, the release must be signed with a
trusted Windows code-signing certificate. Media Browser's GitHub Actions
release workflow supports optional signing when these repository secrets are
configured:

    WINDOWS_CODE_SIGN_CERT_BASE64
    WINDOWS_CODE_SIGN_CERT_PASSWORD
