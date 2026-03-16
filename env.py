import os

def get_env_variable(key):
    value = os.getenv(key)
    if value is None:
        raise RuntimeError(f"Environment variable {key} is not set")
    return value
