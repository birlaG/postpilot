class PublishError(Exception):
    """Raised by adapters. retryable=False means retrying cannot help (bad token, unsupported)."""

    def __init__(self, message, retryable=True, auth_failed=False):
        super().__init__(message)
        self.retryable = retryable
        self.auth_failed = auth_failed


class Publisher:
    """One adapter per platform. Keep the interface identical so the engine never changes
    when a platform is added (PRD section 8)."""

    platform = ""
    implemented = True

    def check(self, account) -> None:
        """Raise PublishError if the stored credentials do not work."""
        raise NotImplementedError

    def publish(self, account, post) -> str:
        """Publish and return the platform's post id."""
        raise NotImplementedError

    def fetch_metrics(self, account, post) -> dict:
        return {}


class NotImplementedPublisher(Publisher):
    implemented = False

    def __init__(self, platform):
        self.platform = platform

    def check(self, account):
        raise PublishError(f"{self.platform} publishing is not implemented yet", retryable=False)

    def publish(self, account, post):
        raise PublishError(f"{self.platform} publishing is not implemented yet", retryable=False)
