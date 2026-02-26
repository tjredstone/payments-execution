import os
from dataclasses import dataclass


@dataclass(frozen=True)
class TrueLayerConfig:
    client_id: str
    client_secret: str
    redirect_uri: str

    # Sandbox endpoints
    auth_base: str = "https://auth.truelayer-sandbox.com"
    api_base: str = "https://api.truelayer-sandbox.com"


def load_truelayer_config() -> TrueLayerConfig:
    return TrueLayerConfig(
        client_id=os.environ["TRUELAYER_CLIENT_ID"],
        client_secret=os.environ["TRUELAYER_CLIENT_SECRET"],
        redirect_uri=os.environ["TRUELAYER_REDIRECT_URI"],
    )
