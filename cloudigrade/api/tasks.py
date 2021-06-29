"""
Celery tasks for use in the api v2 app.

Important notes for developers:

If you find yourself adding a new Celery task, please be aware of how Celery
determines which queue to read and write to work on that task. By default,
Celery tasks will go to a queue named "celery". If you wish to separate a task
onto a different queue (which may make it easier to see the volume of specific
waiting tasks), please be sure to update all the relevant configurations to
use that custom queue. This includes CELERY_TASK_ROUTES in config and the
Celery worker's --queues argument (see related openshift deployment config files
elsewhere and in related repos like e2e-deploy and saas-templates).

Please also include a specific name in each task decorator. If a task function
is ever moved in the future, but it was previously using automatic names, that
will cause a problem if Celery tries to execute an instance of a task that was
created *before* the function moved. Why? The old automatic name will not match
the new automatic name, and Celery cannot know that the two were once the same.
Therefore, we should always preserve the original name in each task function's
decorator even if the function itself is renamed or moved elsewhere.
"""
import json
import logging
from datetime import timedelta

from celery import shared_task
from dateutil import parser as date_parser
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils.translation import gettext as _
from requests.exceptions import BaseHTTPError, RequestException

from api import error_codes
from api.clouds.aws.tasks import (
    CLOUD_KEY,
    CLOUD_TYPE_AWS,
    configure_customer_aws_and_create_cloud_account,
    scale_down_cluster,
)
from api.clouds.aws.util import (
    persist_aws_inspection_cluster_results,
    start_image_inspection,
    update_aws_cloud_account,
)
from api.models import (
    CloudAccount,
    ConcurrentUsageCalculationTask,
    Instance,
    InstanceEvent,
    MachineImage,
    Run,
    UserTaskLock,
)
from api.util import (
    calculate_max_concurrent_usage,
    calculate_max_concurrent_usage_from_runs,
    normalize_runs,
    recalculate_runs,
    schedule_concurrent_calculation_task,
)
from util import aws
from util.celery import retriable_shared_task
from util.exceptions import AwsThrottlingException, KafkaProducerException
from util.misc import get_now, lock_task_for_user_ids
from util.redhatcloud import sources

logger = logging.getLogger(__name__)


@retriable_shared_task(
    autoretry_for=(RequestException, BaseHTTPError, AwsThrottlingException),
    name="api.tasks.create_from_sources_kafka_message",
)
@aws.rewrap_aws_errors
def create_from_sources_kafka_message(message, headers):
    """
    Create our model objects from the Sources Kafka message.

    Because the Sources API may not always be available, this task must
    gracefully retry if communication with Sources fails unexpectedly.

    If this function succeeds, it spawns another async task to set up the
    customer's AWS account (configure_customer_aws_and_create_cloud_account).

    Args:
        message (dict): the "value" attribute of a message from a Kafka
            topic generated by the Sources service and having event type
            "ApplicationAuthentication.create"
        headers (list): the headers of a message from a Kafka topic
            generated by the Sources service and having event type
            "ApplicationAuthentication.create"

    """
    authentication_id = message.get("authentication_id", None)
    application_id = message.get("application_id", None)
    (
        account_number,
        platform_id,
    ) = sources.extract_ids_from_kafka_message(message, headers)

    if account_number is None or authentication_id is None or application_id is None:
        logger.error(_("Aborting creation. Incorrect message details."))
        return

    application = sources.get_application(account_number, application_id)
    if not application:
        logger.info(
            _(
                "Application ID %(application_id)s for account number "
                "%(account_number)s does not exist; aborting cloud account creation."
            ),
            {"application_id": application_id, "account_number": account_number},
        )
        return

    application_type = application["application_type_id"]
    if application_type is not sources.get_cloudigrade_application_type_id(
        account_number
    ):
        logger.info(_("Aborting creation. Application Type is not cloudmeter."))
        return

    authentication = sources.get_authentication(account_number, authentication_id)

    if not authentication:
        error_code = error_codes.CG2000
        error_code.log_internal_message(
            logger,
            {"authentication_id": authentication_id, "account_number": account_number},
        )
        error_code.notify(account_number, application_id)
        return

    authtype = authentication.get("authtype")
    if authtype not in settings.SOURCES_CLOUDMETER_AUTHTYPES:
        error_code = error_codes.CG2001
        error_code.log_internal_message(
            logger, {"authentication_id": authentication_id, "authtype": authtype}
        )
        error_code.notify(account_number, application_id)
        return

    resource_type = authentication.get("resource_type")
    resource_id = authentication.get("resource_id")
    if resource_type != settings.SOURCES_RESOURCE_TYPE:
        error_code = error_codes.CG2002
        error_code.log_internal_message(
            logger, {"resource_id": resource_id, "account_number": account_number}
        )
        error_code.notify(account_number, application_id)
        return

    source_id = application.get("source_id")
    arn = authentication.get("username") or authentication.get("password")

    if not arn:
        error_code = error_codes.CG2004
        error_code.log_internal_message(
            logger, {"authentication_id": authentication_id}
        )
        error_code.notify(account_number, application_id)
        return

    with transaction.atomic():
        user, created = User.objects.get_or_create(username=account_number)
        if created:
            user.set_unusable_password()
            logger.info(
                _("User %s was not found and has been created."),
                account_number,
            )
            UserTaskLock.objects.get_or_create(user=user)

    # Conditionalize the logic for different cloud providers
    if authtype == settings.SOURCES_CLOUDMETER_ARN_AUTHTYPE:
        configure_customer_aws_and_create_cloud_account.delay(
            user.username,
            arn,
            authentication_id,
            application_id,
            source_id,
        )


@retriable_shared_task(
    autoretry_for=(RuntimeError, AwsThrottlingException),
    name="api.tasks.delete_from_sources_kafka_message",
)
@aws.rewrap_aws_errors
def delete_from_sources_kafka_message(message, headers):
    """
    Delete our cloud account as per the Sources Kafka message.

    This function is decorated to retry if an unhandled `RuntimeError` is
    raised, which is the exception we raise in `rewrap_aws_errors` if we
    encounter an unexpected error from AWS. This means it should keep retrying
    if AWS is misbehaving.

    Args:
        message (dict): a message from the Kafka topic generated by the
            Sources service and having event type "Authentication.destroy"
        headers (list): the headers of a message from a Kafka topic
            generated by the Sources service and having event type
            "Authentication.destroy" or "Source.destroy"

    """
    (
        account_number,
        platform_id,
    ) = sources.extract_ids_from_kafka_message(message, headers)

    logger.info(
        _(
            "delete_from_sources_kafka_message for account_number %(account_number)s, "
            "platform_id %(platform_id)s"
        ),
        {
            "account_number": account_number,
            "platform_id": platform_id,
        },
    )

    if account_number is None or platform_id is None:
        logger.error(_("Aborting deletion. Incorrect message details."))
        return

    authentication_id = message["authentication_id"]
    application_id = message["application_id"]
    query_filter = Q(
        platform_application_id=application_id,
        platform_authentication_id=authentication_id,
    )

    logger.info(_("Deleting CloudAccounts using filter %s"), query_filter)
    cloud_accounts = CloudAccount.objects.filter(query_filter)
    _delete_cloud_accounts(cloud_accounts)


@retriable_shared_task(
    autoretry_for=(RuntimeError, AwsThrottlingException),
    name="api.tasks.delete_cloud_account",
)
@aws.rewrap_aws_errors
def delete_cloud_account(cloud_account_id):
    """
    Delete the CloudAccount with the given ID.

    This task function exists to support an internal API for deleting a CloudAccount.
    Unfortunately, deletion may be a time-consuming operation and needs to be done
    asynchronously to avoid http request handling timeouts.

    Args:
        cloud_account_id (int): the cloud account ID

    """
    logger.info(_("Deleting CloudAccount with ID %s"), cloud_account_id)
    cloud_accounts = CloudAccount.objects.filter(id=cloud_account_id)
    _delete_cloud_accounts(cloud_accounts)


def _delete_cloud_accounts(cloud_accounts):
    """
    Delete the given list of CloudAccount objects.

    Args:
        cloud_accounts (list[CloudAccount]): cloud accounts to delete

    """
    for cloud_account in cloud_accounts:
        # Lock on the user level, so that a single user can only have one task
        # running at a time.
        #
        # The select_for_update() lock has been moved from the CloudAccount to the
        # UserTaskLock. We should release the UserTaskLock with each
        # cloud_account.delete action.
        #
        # Using the UserTaskLock *should* fix the issue of Django not getting a
        # row-level lock in the DB for each CloudAccount we want to delete until
        # after all of the pre_delete logic completes
        with lock_task_for_user_ids([cloud_account.user.id]):
            # Call delete on the CloudAccount queryset instead of the specific
            # cloud_account. Why? A queryset delete does not raise DoesNotExist
            # exceptions if the cloud_account has already been deleted.
            # If we call delete on a nonexistent cloud_account, we run into trouble
            # with Django rollback and our task lock.
            # See https://gitlab.com/cloudigrade/cloudigrade/-/merge_requests/811
            try:
                cloud_account.refresh_from_db()
                CloudAccount.objects.filter(id=cloud_account.id).delete()
            except CloudAccount.DoesNotExist:
                logger.info(
                    _("Cloud Account %s has already been deleted"), cloud_account
                )


@retriable_shared_task(
    autoretry_for=(
        RequestException,
        BaseHTTPError,
        RuntimeError,
        AwsThrottlingException,
    ),
    name="api.tasks.update_from_source_kafka_message",
)
@aws.rewrap_aws_errors
def update_from_source_kafka_message(message, headers):
    """
    Update our model objects from the Sources Kafka message.

    Because the Sources API may not always be available, this task must
    gracefully retry if communication with Sources fails unexpectedly.

    This function is also decorated to retry if an unhandled `RuntimeError` is
    raised, which is the exception we raise in `rewrap_aws_errors` if we
    encounter an unexpected error from AWS. This means it should keep retrying
    if AWS is misbehaving.

    Args:
        message (dict): the "value" attribute of a message from a Kafka
            topic generated by the Sources service and having event type
            "Authentication.update"
        headers (list): the headers of a message from a Kafka topic
            generated by the Sources service and having event type
            "Authentication.update"

    """
    (
        account_number,
        authentication_id,
    ) = sources.extract_ids_from_kafka_message(message, headers)

    if account_number is None or authentication_id is None:
        logger.error(_("Aborting update. Incorrect message details."))
        return

    try:
        clount = CloudAccount.objects.get(platform_authentication_id=authentication_id)

        authentication = sources.get_authentication(account_number, authentication_id)

        if not authentication:
            logger.info(
                _(
                    "Authentication ID %(authentication_id)s for account number "
                    "%(account_number)s does not exist; aborting cloud account update."
                ),
                {
                    "authentication_id": authentication_id,
                    "account_number": account_number,
                },
            )
            return

        resource_type = authentication.get("resource_type")
        application_id = authentication.get("resource_id")
        if resource_type != settings.SOURCES_RESOURCE_TYPE:
            logger.info(
                _(
                    "Resource ID %(resource_id)s for account number %(account_number)s "
                    "is not of type Application; aborting cloud account update."
                ),
                {"resource_id": application_id, "account_number": account_number},
            )
            return

        application = sources.get_application(account_number, application_id)
        source_id = application.get("source_id")

        arn = authentication.get("username") or authentication.get("password")
        if not arn:
            logger.info(_("Could not update CloudAccount with no ARN provided."))
            error_code = error_codes.CG2004
            error_code.log_internal_message(
                logger, {"authentication_id": authentication_id}
            )
            error_code.notify(account_number, application_id)
            return

        # If the Authentication being updated is arn, do arn things.
        # The kafka message does not always include authtype, so we get this from
        # the sources API call
        if authentication.get("authtype") == settings.SOURCES_CLOUDMETER_ARN_AUTHTYPE:
            update_aws_cloud_account(
                clount,
                arn,
                account_number,
                authentication_id,
                source_id,
            )
    except CloudAccount.DoesNotExist:
        # Is this authentication meant to be for us? We should check.
        # Get list of all app-auth objects and filter by our authentication
        response_json = sources.list_application_authentications(
            account_number, authentication_id
        )

        if response_json.get("meta").get("count") > 0:
            for application_authentication in response_json.get("data"):
                create_from_sources_kafka_message.delay(
                    application_authentication, headers
                )
        else:
            logger.info(
                _(
                    "The updated authentication with ID %s and account number %s "
                    "is not managed by cloud meter."
                ),
                authentication_id,
                account_number,
            )


def process_instance_event(event):
    """
    Process instance events that have been saved during log analysis.

    Note:
        When processing power_on type events, this triggers a recalculation of
        ConcurrentUsage objects. If the event is at some point in the
        not-too-recent past, this may take a while as every day since the event
        will get recalculated and saved. We do not anticipate this being a real
        problem in practice, but this has the potential to slow down unit test
        execution over time since their occurred_at values are often static and
        will recede father into the past from "today", resulting in more days
        needing to recalculate. This effect could be mitigated in tests by
        patching parts of the datetime module that are used to find "today".
    """
    after_run = Q(start_time__gt=event.occurred_at)
    during_run = Q(start_time__lte=event.occurred_at, end_time__gt=event.occurred_at)
    during_run_no_end = Q(start_time__lte=event.occurred_at, end_time=None)

    filters = after_run | during_run | during_run_no_end
    instance = Instance.objects.get(id=event.instance_id)

    if Run.objects.filter(filters, instance=instance).exists():
        recalculate_runs(event)
    elif event.event_type == InstanceEvent.TYPE.power_on:
        normalized_runs = normalize_runs([event])
        runs = []
        for index, normalized_run in enumerate(normalized_runs):
            logger.info(
                "Processing run {} of {}".format(index + 1, len(normalized_runs))
            )
            run = Run(
                start_time=normalized_run.start_time,
                end_time=normalized_run.end_time,
                machineimage_id=normalized_run.image_id,
                instance_id=normalized_run.instance_id,
                instance_type=normalized_run.instance_type,
                memory=normalized_run.instance_memory,
                vcpu=normalized_run.instance_vcpu,
            )
            run.save()
            runs.append(run)
        calculate_max_concurrent_usage_from_runs(runs)


@shared_task(name="api.tasks.persist_inspection_cluster_results_task")
@aws.rewrap_aws_errors
def persist_inspection_cluster_results_task():
    """
    Task to run periodically and read houndigrade messages.

    Returns:
        None: Run as an asynchronous Celery task.

    """
    queue_url = aws.get_sqs_queue_url(settings.HOUNDIGRADE_RESULTS_QUEUE_NAME)
    successes, failures = [], []
    for message in aws.yield_messages_from_queue(
        queue_url, settings.AWS_SQS_MAX_HOUNDI_YIELD_COUNT
    ):
        logger.info(_('Processing inspection results with id "%s"'), message.message_id)

        inspection_results = json.loads(message.body)
        if inspection_results.get(CLOUD_KEY) == CLOUD_TYPE_AWS:
            try:
                persist_aws_inspection_cluster_results(inspection_results)
            except Exception as e:
                logger.exception(_("Unexpected error in result processing: %s"), e)
                logger.debug(_("Failed message body is: %s"), message.body)
                failures.append(message)
                continue

            logger.info(
                _("Successfully processed message id %s; deleting from queue."),
                message.message_id,
            )
            aws.delete_messages_from_queue(queue_url, [message])
            successes.append(message)
        else:
            logger.error(
                _('Unsupported cloud type: "%s"'), inspection_results.get(CLOUD_KEY)
            )
            failures.append(message)

    if successes or failures:
        scale_down_cluster.delay()
    else:
        logger.info("No inspection results found.")

    return successes, failures


@shared_task(name="api.tasks.inspect_pending_images")
@transaction.atomic
def inspect_pending_images():
    """
    (Re)start inspection of images in PENDING, PREPARING, or INSPECTING status.

    This generally should not be necessary for most images, but if an image
    inspection fails to proceed normally, this function will attempt to run it
    through inspection again.

    This function runs atomically in a transaction to protect against the risk
    of it being called multiple times simultaneously which could result in the
    same image being found and getting multiple inspection tasks.
    """
    updated_since = get_now() - timedelta(
        seconds=settings.INSPECT_PENDING_IMAGES_MIN_AGE
    )
    restartable_statuses = [
        MachineImage.PENDING,
        MachineImage.PREPARING,
        MachineImage.INSPECTING,
    ]
    images = MachineImage.objects.filter(
        status__in=restartable_statuses,
        instance__aws_instance__region__isnull=False,
        updated_at__lt=updated_since,
    ).distinct()
    logger.info(
        _(
            "Found %(number)s images for inspection that have not updated "
            "since %(updated_time)s"
        ),
        {"number": images.count(), "updated_time": updated_since},
    )

    for image in images:
        instance = image.instance_set.filter(aws_instance__region__isnull=False).first()
        arn = instance.cloud_account.content_object.account_arn
        ami_id = image.content_object.ec2_ami_id
        region = instance.content_object.region
        start_image_inspection(arn, ami_id, region)


@shared_task(
    bind=True,
    default_retry_delay=settings.SCHEDULE_CONCURRENT_USAGE_CALCULATION_DELAY,
    name="api.tasks.calculate_max_concurrent_usage_task",
    track_started=True,
)
def calculate_max_concurrent_usage_task(self, date, user_id):  # noqa: C901
    """
    Schedule a task to calculate maximum concurrent usage of RHEL instances.

    Args:
        self (celery.Task): The bound task. With this we can retry if necessary.
        date (str): the day during which we are measuring usage.
            Celery serializes the date as a string in the format "%Y-%B-%dT%H:%M:%S.
        user_id (int): required filter on user

    Returns:
        ConcurrentUsage for the given date and user ID.

    """
    task_id = self.request.id
    date = date_parser.parse(date).date()

    # Temporary logger.info to help diagnose retry issues.
    logger.info(
        "retries is %(retries)s for id %(id)s user_id %(user_id)s and date %(date)s.",
        {
            "retries": self.request.retries,
            "id": task_id,
            "user_id": user_id,
            "date": date,
        },
    )

    # If the user does not exist, all the related ConcurrentUsage
    # objects should also have been removed, so we can exit early.
    if not User.objects.filter(id=user_id).exists():
        return

    try:
        # Lock the task at a user level. A user can only run one task at a time.
        # Since this both starts a transaction and blocks any others from starting, we
        # can be reasonably confident that there are no other tasks processing for the
        # same user and date at the same time.
        with lock_task_for_user_ids([user_id]):
            try:
                calculation_task = ConcurrentUsageCalculationTask.objects.get(
                    task_id=task_id
                )
            except ConcurrentUsageCalculationTask.DoesNotExist:
                # It's possible but unlikely this task was deleted since its task was
                # delayed. Since the same user still exists, try scheduling a new task.
                logger.warning(
                    "ConcurrentUsageCalculationTask not found for task ID %(task_id)s! "
                    "Scheduling a new task for user_id %(user_id)s and date %(date)s.",
                    {"task_id": task_id, "user_id": user_id, "date": date},
                )
                schedule_concurrent_calculation_task(date, user_id)
                return

            if calculation_task.status != ConcurrentUsageCalculationTask.SCHEDULED:
                # It's possible but unlikely that something else has changed the status
                # of this task. If it's not currently SCHEDULED, log and return early.
                logger.info(
                    "ConcurrentUsageCalculationTask for task ID %(task_id)s for "
                    "user_id %(user_id)s and date %(date)s has status "
                    "%(status)s which is not SCHEDULED.",
                    {
                        "user_id": user_id,
                        "date": date,
                        "task_id": task_id,
                        "status": calculation_task.status,
                    },
                )
                return

            calculate_max_concurrent_usage(date, user_id)

            calculation_task.status = ConcurrentUsageCalculationTask.COMPLETE
            calculation_task.save()
            logger.info(
                "Completed calculate_max_concurrent_usage_task for user_id %(user_id)s "
                "and date %(date)s (task_id %(task_id)s).",
                {"user_id": user_id, "date": date, "task_id": task_id},
            )
            return
    except Exception as unknown_exception:
        # It's unclear exactly what other exceptions might arise, but just to be safe,
        # let's log the trace, set the task's status to ERROR, and re-raise it.
        logger.warning(unknown_exception, exc_info=True)
        # Use this objects.filter().update() pattern so that we don't risk raising an
        # IntegrityError in case the object has somehow been deleted.
        ConcurrentUsageCalculationTask.objects.filter(task_id=task_id).update(
            status=ConcurrentUsageCalculationTask.ERROR
        )
        raise unknown_exception


@transaction.atomic()
def _delete_user(user):
    """Delete given User if it has no related CloudAccount objects."""
    if CloudAccount.objects.filter(user_id=user.id).exists():
        return False

    count, __ = user.delete()
    return count > 0


@shared_task(name="api.tasks.delete_inactive_users")
def delete_inactive_users():
    """
    Delete all inactive User objects.

    A User is considered to be inactive if all of the following are true:
    - the User has no related CloudAccount objects
    - the User is not a superuser
    - the User's date joined is more than MINIMUM_USER_AGE_SECONDS old
    """
    oldest_allowed_date_joined = get_now() - timedelta(
        seconds=settings.DELETE_INACTIVE_USERS_MIN_AGE
    )
    users = User.objects.filter(
        is_superuser=False, date_joined__lt=oldest_allowed_date_joined
    )
    total_user_count = users.count()
    deleted_user_count = 0
    logger.info(
        _(
            "Found %(total_user_count)s not-superuser Users joined before "
            "%(date_joined)s."
        ),
        {
            "total_user_count": total_user_count,
            "date_joined": oldest_allowed_date_joined,
        },
    )
    for user in users:
        if _delete_user(user):
            deleted_user_count += 1
    logger.info(
        _(
            "Successfully deleted %(deleted_user_count)s of %(total_user_count)s "
            "users."
        ),
        {
            "deleted_user_count": deleted_user_count,
            "total_user_count": total_user_count,
        },
    )


@shared_task(name="api.tasks.enable_account")
def enable_account(cloud_account_id):
    """
    Task to enable a cloud account.

    Returns:
        None: Run as an asynchronous Celery task.

    """
    try:
        cloud_account = CloudAccount.objects.get(id=cloud_account_id)
    except CloudAccount.DoesNotExist:
        logger.warning(
            "Cloud Account with ID %(cloud_account_id)s does not exist. "
            "No cloud account to enable, exiting.",
            {"cloud_account_id": cloud_account_id},
        )
        return

    cloud_account.enable()


@retriable_shared_task(
    autoretry_for=(KafkaProducerException,),
    name="api.tasks.notify_application_availability_task",
)
def notify_application_availability_task(
    account_number, application_id, availability_status, availability_status_error=""
):
    """
    Update Sources application's availability status.

    This is a task wrapper to the sources.notify_application_availability
    method which sends the availability_status Kafka message to Sources.

    Args:
        account_number (str): Account number identifier
        application_id (int): Platform insights application id
        availability_status (string): Availability status to set
        availability_status_error (string): Optional status error
    """
    try:
        sources.notify_application_availability(
            account_number,
            application_id,
            availability_status,
            availability_status_error,
        )
    except KafkaProducerException:
        raise
