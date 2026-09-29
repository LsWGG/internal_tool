DBX arm64 static browser package

Run:
  ./dbx

Then open:
  http://127.0.0.1:4224

This package runs a musl-linked static dbx-web binary and serves the bundled
frontend from ./dist. The backend binary has no ELF interpreter and no DT_NEEDED
shared library entries, so it runs on any Linux distribution regardless of the
system glibc version (verified down to Ubuntu 14.04).

Useful environment variables:
  DBX_PORT=4224
  DBX_DATA_DIR=./data
  DBX_PASSWORD=your-password
  DBX_DISABLE_PASSWORD=1
