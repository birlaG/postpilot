from django.utils import timezone


class WorkspaceMiddleware:
    """Attach request.workspace and request.member for the logged-in user (first workspace)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.workspace = None
        request.member = None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            member = user.memberships.select_related("workspace__brand", "workspace__subscription__plan").first()
            if member:
                request.member, request.workspace = member, member.workspace
                timezone.activate(request.workspace.tz)
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
