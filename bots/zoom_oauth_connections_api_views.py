from drf_spectacular.openapi import OpenApiResponse
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response

from .authentication import ApiKeyAuthentication
from .serializers import CreateZoomOAuthConnectionSerializer, ZoomOAuthConnectionSerializer
from .tasks.sync_zoom_oauth_connection_task import enqueue_sync_zoom_oauth_connection_task
from .throttling import ProjectPostThrottle
from .zoom_oauth_connections_api_utils import create_zoom_oauth_connection

TokenHeaderParameter = [
    OpenApiParameter(
        name="Authorization",
        type=str,
        location=OpenApiParameter.HEADER,
        description="API key for authentication",
        required=True,
        default="Token YOUR_API_KEY_HERE",
    ),
    OpenApiParameter(
        name="Content-Type",
        type=str,
        location=OpenApiParameter.HEADER,
        description="Should always be application/json",
        required=True,
        default="application/json",
    ),
]

NewlyCreatedZoomOAuthConnectionExample = OpenApiExample(
    "Newly Created Zoom OAuth Connection",
    value={
        "id": "zoc_abcdef1234567890",
        "zoom_oauth_app": "zoa_abcdef1234567890",
        "state": "connected",
        "metadata": {"tenant_id": "1234567890"},
        "user_id": "user_abcdef1234567890",
        "account_id": "account_abcdef1234567890",
        "connection_failure_data": None,
        "created_at": "2025-01-13T10:30:00.123456Z",
        "updated_at": "2025-01-13T10:30:00.123456Z",
    },
    description="Example response when a zoom oauth connection is successfully created",
)


class ZoomOAuthConnectionCursorPagination(CursorPagination):
    ordering = "-created_at"
    page_size = 25


class ZoomOAuthConnectionListCreateView(GenericAPIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]
    pagination_class = ZoomOAuthConnectionCursorPagination
    serializer_class = ZoomOAuthConnectionSerializer

    @extend_schema(
        operation_id="Create Zoom OAuth Connection",
        summary="Create a new zoom oauth connection",
        description="After being created, the zoom oauth connection will be used.",
        request=CreateZoomOAuthConnectionSerializer,
        responses={
            201: OpenApiResponse(
                response=ZoomOAuthConnectionSerializer,
                description="Zoom OAuth Connection created successfully",
                examples=[NewlyCreatedZoomOAuthConnectionExample],
            ),
            400: OpenApiResponse(description="Invalid input"),
        },
        parameters=TokenHeaderParameter,
        tags=["Zoom OAuth Connections"],
    )
    def post(self, request):
        zoom_oauth_connection, error = create_zoom_oauth_connection(data=request.data, project=request.auth.project)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        # Immediately sync the zoom oauth connection
        enqueue_sync_zoom_oauth_connection_task(zoom_oauth_connection)

        return Response(ZoomOAuthConnectionSerializer(zoom_oauth_connection).data, status=status.HTTP_201_CREATED)
