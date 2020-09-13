import json
import datetime
import os
import logging
from boto3 import client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
region = 'eu-west-1'


def paginate_operation(client: client, operation: str, **operation_parameters):
    result = {}
    paginator = client.get_paginator(operation)
    page_iterator = paginator.paginate(**operation_parameters)
    for page in page_iterator:
        result.update(page)
    return result


def datetime_to_str(o: datetime.datetime):
    if isinstance(o, datetime.datetime):
        return o.__str__()


def create_dict_payload(servicediscovery_client: client, namespace_id: str):
    """
    Create a Cloudmap Services payload
    """
    payload = {}
    services = paginate_operation(servicediscovery_client, 'list_services', Filters=[
        {
            'Name': 'NAMESPACE_ID',
            'Values': [namespace_id, ],
            'Condition': 'EQ'
        }])
    logging.info(f"nr. of services found: {str(len(services['Services']))}")
    payload.update(services)
    logging.info('retreiving instances and tags for each service')
    for service in payload['Services']:
        instances = paginate_operation(servicediscovery_client,
                                       'list_instances',
                                       ServiceId=service['Id'])
        service.update(instances)
        tags = servicediscovery_client.list_tags_for_resource(
            ResourceARN=service['Arn'])
        service.update(tags)
    return (payload)


def send_message(sqs_client: client, queue_url: str, message_body: str):
    response = sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=message_body
    )
    return response


def lambda_handler(event, context):
    """
    Create a Cloudmap Services payload with
        - list_services
        - for each service lookup tag and add to service dict
    Send SQS message with payload
    """

    servicediscovery_client = client('servicediscovery')
    namespace_id = os.environ['ENV_NAMESPACE_ID']
    queue_url = os.environ['ENV_QUEUE_URL']

    dict_payload = create_dict_payload(servicediscovery_client, namespace_id)
    json_payload = json.dumps(dict_payload, default=datetime_to_str)

    logging.info('Send message with payload to queue:')
    logging.info(str(json_payload))
    sqs_client = client('sqs')
    send_message(sqs_client, queue_url, json_payload)
