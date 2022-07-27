import sentry_sdk
from flask import Flask
from sentry_sdk.integrations.flask import FlaskIntegration

from import_system_symbols_from_ipsw import import_symbols

sentry_sdk.init(
    dsn="https://f86a0e29c86e49688d691e194c5bf9eb@o1.ingest.sentry.io/6418660",
    traces_sample_rate=1.0,
    integrations=[
        FlaskIntegration(),
    ],
)
app = Flask(__name__)


@app.route("/", defaults={"os_name": "ios", "os_version": "latest"})
@app.route("/<os_name>", defaults={"os_version": "latest"})
@app.route("/<os_name>/<os_version>")
def import_symbols_for_os(os_name, os_version):
    import_symbols(os_name, os_version)
    return "", 204
