"""util.exceptions module."""
import http
import logging

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.utils.translation import gettext as _
from rest_framework.exceptions import APIException, NotFound, ParseError
from rest_framework.exceptions import PermissionDenied as DrfPermissionDenied
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


class InvalidArn(ParseError):
    """an invalid ARN was detected."""


class NotReadyException(Exception):
    """Something was not ready and may need later retry."""

    def __init__(self, message=None):
        """Log the exception message upon creation."""
        logger.info('%s: %s', self.__class__.__name__, message)
        self.message = message


class AwsSnapshotCopyLimitError(NotReadyException):
    """The limit on concurrent snapshot copy operations has been met."""


class AwsSnapshotNotOwnedError(NotReadyException):
    """Raise when our account id does not have permissions on the snapshot."""


class AwsSnapshotOwnedError(NotReadyException):
    """Raise if account id has permissions on the snapshot after removal."""


class ImageNotReadyException(NotReadyException):
    """The requested image was not ready."""


class SnapshotNotReadyException(NotReadyException):
    """The requested snapshot was not ready."""


class AwsImageError(Exception):
    """The requested image is in a failed state."""


class AwsSnapshotError(Exception):
    """The requested snapshot is in an error state."""


class AwsVolumeNotReadyError(NotReadyException):
    """The requested volume was not ready."""


class AwsVolumeError(Exception):
    """The requested volume is not in an acceptable state."""


class AwsAutoScalingGroupNotFound(Exception):
    """The requested AutoScaling group was not found."""


class AwsNonZeroAutoScalingGroupSize(Exception):
    """Raise when AWS auto scaling group has nonzero size."""


class AwsECSInstanceNotReady(NotReadyException):
    """Raise when AWS ECS Container Instance is not yet ready."""


class AwsTooManyECSInstances(NotReadyException):
    """Raise when there are too many AWS ECS Container Instances."""


class MaximumNumberOfTrailsExceededException(APIException):
    """Raise when the max number of cloud trails exceeds the limit."""

    status_code = http.HTTPStatus.CONFLICT
    default_detail = _('Exceeded maximum number of cloud trails')


class InvalidHoundigradeJsonFormat(Exception):
    """Raise when houndigrade returns json that does not have images."""


class CloudTrailCannotStopLogging(APIException):
    """Raise when we cannot stop cloud trail logging on client account."""

    status_code = http.HTTPStatus.INTERNAL_SERVER_ERROR


class NormalizeRunException(APIException):
    """Raise when something unexpected happens in building NormalizeRuns."""

    status_code = http.HTTPStatus.INTERNAL_SERVER_ERROR


class NotImplementedAPIException(APIException):
    """Raise when we encounter NotImplementedError."""

    status_code = http.HTTPStatus.NOT_IMPLEMENTED


class AwsPolicyCreationException(Exception):
    """Raise when AWS Policy fails creation unexpectedly."""


def api_exception_handler(exc, context):
    """
    Log exception and return an appropriately formatted response.

    For any exception that doesn't specifically extend DRF's base APIException
    class, we deliberately suppress the original exception and raise a plain
    instance of APIException or a specific subclass of APIException. We do this
    because we do not want to leak details about the underlying error and
    system state to the end user.
    """
    if isinstance(exc, Http404):
        exc = NotFound()
    elif isinstance(exc, PermissionDenied):
        exc = DrfPermissionDenied()
    elif isinstance(exc, NotImplementedError):
        logger.exception(exc)
        exc = NotImplementedAPIException()
    elif not isinstance(exc, APIException):
        logger.exception(exc)
        exc = APIException()
    return exception_handler(exc, context)
