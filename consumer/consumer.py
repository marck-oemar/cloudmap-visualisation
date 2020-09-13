import os
import json
import logging
from boto3 import client
from neomodel import (config, StructuredNode, StringProperty,
                      RelationshipTo)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

region = 'eu-west-1'

# neo4j database version < 4. Neomodel doesn't play well with version 4 yet.
#config.DATABASE_URL = 'bolt://neo4j:neo4jneo4j@localhost:7687'
config.DATABASE_URL = os.environ['NEO4J_URL']
config.ENCRYPTED_CONNECTION = False


class Service(StructuredNode):
    '''
    Service, a neo4j structurednode
    '''
    name = StringProperty(unique_index=True, required=True)
    update_id = StringProperty(index=False, required=True)
    service = RelationshipTo('Service', 'DEP_ON')


class Instance(StructuredNode):
    '''
    Instance, a neo4j structurednode
    '''
    name = StringProperty(unique_index=True, required=True)
    update_id = StringProperty(index=False, required=True)
    service = RelationshipTo(Service, 'REGISTERED')


def acquire_lock(ddb_client: client, table_name: str, lock_item_id: str):
    '''
    Get a lock using an dynamodb item. Used for single concurrency with neo4j database
    '''
    # is there a lock item? if not, create initial lock item
    try:
        lock_item = ddb_client.get_item(
            TableName=table_name,
            Key={
                'id': {'S': lock_item_id}
            }
        )['Item']
    except KeyError:
        # lock item doesn't exist, so at least there is no active lock
        # let's create initial lock item, and acquire the lock
        ddb_client.put_item(
            TableName=table_name,
            Item={
                'id': {'S': lock_item_id},
                'lock_bool': {'BOOL': True},
                'version': {'N': '1'}
            },
            ConditionExpression='attribute_not_exists(id)'
        )
    else:
        # aquire lock
        try:
            logging.info(
                f"Acquiring lock")
            current_version = int(lock_item['version']['N'])
            ddb_client.update_item(
                TableName=table_name,
                Key={
                    'id': {'S': lock_item_id}
                },
                ExpressionAttributeValues={
                    ':current_version': {'N': str(current_version)},
                    ':new_version': {'N': str(current_version + 1)},
                    ':lock_bool_true': {'BOOL': True},
                    ':lock_bool_false': {'BOOL': False},
                },
                UpdateExpression="SET lock_bool=:lock_bool_true, version=:new_version",
                ConditionExpression="(version=:current_version) and (lock_bool=:lock_bool_false)"
            )
        except Exception as err:
            raise Exception(f'Cannot aquire lock. Error message: {err}')


def release_lock(ddb_client, table_name, lock_item_id):
    '''
    Release a lock using an dynamodb item. Used for single concurrency with neo4j database
    '''
    # release lock
    logging.info(
        f"Releasing lock")
    ddb_client.update_item(
        TableName=table_name,
        Key={
            'id': {'S': lock_item_id}
        },
        ExpressionAttributeValues={
            ':lock_bool_true': {'BOOL': True},
            ':lock_bool_false': {'BOOL': False},
        },
        UpdateExpression="SET lock_bool=:lock_bool_false",
        ConditionExpression="(lock_bool=:lock_bool_true)"
    )


def connect_service_rel_to_service(service: dict, tags_list: list, tag_key: str):
    '''
        create a relationship from service to another service. (for instance frontend to backend)
    '''
    service_node = Service.nodes.get(name=service['Name'])

    # Connect relationship to depending service
    relationship_to_service = None
    for tag in tags_list:
        if tag['Key'] == tag_key:
            relationship_to_service = tag['Value']
    if relationship_to_service is not None:
        logging.info(
            f"Found service tag with relationship to service: {relationship_to_service}")
        # let's get the service we want a relationship with
        try:
            to_service_node = Service.nodes.get(name=relationship_to_service)
        except Service.DoesNotExist:
            logging.warning(
                f"Relationship_to_service {relationship_to_service} does not exist")
        else:
            logging.info(
                f"Creating relationship_to_service {relationship_to_service} ...")
            service_node.service.connect(to_service_node)


def merge_s(service: dict, update_id: str):
    '''
    Merge (create or update) a Service node
    '''
    try:
        service_node = Service.nodes.get(name=service['Name'])
    except Service.DoesNotExist:
        logging.info(
            f"Service {service['Name']} does not exists, creating now")
        service_node = Service(
            name=service['Name'], update_id=update_id).save()
    else:
        logging.info(
            f"Updating Service {service['Name']}")
        service_node.update_id = update_id
        service_node.save()


def merge_instance(service: dict, instance: dict, update_id: str):
    '''
    Merge (create or update) an Instance node
    '''
    service_node = Service.nodes.get(name=service['Name'])

    try:
        instance_node = Instance.nodes.get(name=instance['Id'])
    except Instance.DoesNotExist:
        logging.info(
            f"Instance {instance['Id']} does not exists, creating now")
        instance_node = Instance(
            name=instance['Id'], update_id=update_id).save()
    else:
        logging.info(
            f"Updating Instance {instance['Id']}")
        instance_node.update_id = update_id
        instance_node.save()
    # now lets connect relationship to service
    logging.info(
        f"Creating instance relationship to service {service_node} ...")
    instance_node.service.connect(service_node)


def update_neo4j(services_list: list, update_id: str):
    '''
    Update all service and instance nodes in neo4j and establish relationships
    '''
    for service in services_list:
        # merge services
        merge_s(service, update_id)

        # Merge instances
        for instance in service['Instances']:
            merge_instance(service, instance, update_id)

    # Now that we have all services created, let's create relationship to depending service
    for service in services_list:
        connect_service_rel_to_service(
            service, service['Tags'], 'NEO4J_RELATIONSHIP_TO_SERVICE')


def clean_neo4j(update_id: str):
    '''
    Cleanup all service and instance nodes and relationships that are obsolete, using the update_id that all nodes have
    '''
    all_instances = Instance.nodes.all()
    for instance in all_instances:
        if instance.update_id != update_id:
            logging.info(f"Instance {instance.name} is obselete, deleting...")
            instance.delete()

    all_services = Service.nodes.all()
    for service in all_services:
        if service.update_id != update_id:
            logging.info(f"Service {service.name} is obselete, deleting...")
            service.delete()


def lambda_handler(event, context):
    """
    Queue consumer that parses the CloudMap services payload and updates the neo4j database
    The consumer will create/update all services and instances from payload and after that clean up any existing nodes that are not present in payload.
    Since this is an atomic transaction, we need single concurrency for the neo4j database
    Features:
    - only continue if lock is available
    - use SQS message id as identifier for transaction
    - use Service Tag NEO4J_RELATIONSHIP_TO to define relationship to another Service
    """

    if len(event['Records']) > 1:
        raise Exception(
            'Event payload contains more than 1 message record in Record list. I can only process 1 message concurrently')

    lock_item_id = os.environ['ENV_LOCK_ITEM_ID']
    table_name = os.environ['ENV_TABLE_NAME']

    sqs_payload = event['Records'][0]
    curr_update_id = sqs_payload['messageId']
    logging.info(f"Transaction update_id: {curr_update_id}")
    services_list = json.loads(sqs_payload['body'])['Services']

    ddb_client = client('dynamodb')
    acquire_lock(ddb_client, table_name, lock_item_id)
    try:
        update_neo4j(services_list, curr_update_id)
        clean_neo4j(curr_update_id)
    except Exception as err:
        release_lock(ddb_client, table_name, lock_item_id)
        raise Exception(f"Exception during update neo4j: {err}")
    else:
        release_lock(ddb_client, table_name, lock_item_id)


if __name__ == '__main__':
    # example events for test purpose

    # full event 1

    event = {
        'Records': [{
            'messageId': 'a484325d-9d37-46f9-a0c9-d75fee458d09',
            'receiptHandle': 'AQEBFaGqxIa7QGgjTzXCAanW7qJdsNz++WcWCCeXySSA5Xi7O1H7J+LRPiIuSN3OhgZN2+aTqSmvXdW4yoUYT6VPzWAA0vTZ+Y1GdBi97TKe7KwnB5sPVfcOR4n4LsLCLqXtoYNDTuCMEu8w9Bhz5rq/FawJNLta6iG+UDCdlEIOS1rKdzH6lvWki7EN+d4aee0G5LtYWwlcsHEp7IQCEt+/BHO9soIblYte/64lARLTfUX8osRnVsG4VidNTcKcV4mzp+kLmWaX/Zg7vddNvN811Sp9stsX4yL98NAh098nAfvFk9tOfEbWk0Z4VKJvTmbubTDR3ircb2lsnW9fvf4dOa6aDVj7XcGOr//vDmukw3XSbmvGh996dfgg+36nZP0j',
            'body': '{"Services": [{"Id": "srv-ktawbepgisbdvh6f", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-ktawbepgisbdvh6f", "Name": "SrvFrontInsu", "Description": "Insurance Frontend", "DnsConfig": {}, "CreateDate": "2020-09-13 16:16:23.502000+02:00", "Instances": [{"Id": "InsFrontInsu", "Attributes": {"Name": "InsFrontInsu"}}], "ResponseMetadata": {"RequestId": "2fac3bfd-02bb-4cbc-845c-24a6bf405c08", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 14:17:12 GMT", "x-amzn-requestid": "2fac3bfd-02bb-4cbc-845c-24a6bf405c08", "content-length": "72", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": [{"Key": "NEO4J_RELATIONSHIP_TO_SERVICE", "Value": "SrvBackInsu"}]}, {"Id": "srv-lt7nd437bx7iuwkn", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-lt7nd437bx7iuwkn", "Name": "lambda-service", "Description": "Example Lambda service", "DnsConfig": {}, "CreateDate": "2020-06-07 12:47:41.489000+02:00", "Instances": [{"Id": "lambda-service_Instance", "Attributes": {"Arn": "arn:aws:lambda:eu-west-1:132017519464:function:lambda-service-ServiceFunction-KCJXDS03YTX"}}], "ResponseMetadata": {"RequestId": "49511866-1471-4476-9015-d7df87221b2a", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 14:17:13 GMT", "x-amzn-requestid": "49511866-1471-4476-9015-d7df87221b2a", "content-length": "11", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": []}, {"Id": "srv-n6wch4ummewuuqns", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-n6wch4ummewuuqns", "Name": "SrvFrontMort", "Description": "Mortgage Frontend", "DnsConfig": {}, "CreateDate": "2020-09-13 16:16:23.424000+02:00", "Instances": [{"Id": "InsFrontMort", "Attributes": {"Name": "InsFrontMort"}}], "ResponseMetadata": {"RequestId": "a2f9d2aa-1d20-457f-b2be-b77db4184510", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 14:17:13 GMT", "x-amzn-requestid": "a2f9d2aa-1d20-457f-b2be-b77db4184510", "content-length": "72", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": [{"Key": "NEO4J_RELATIONSHIP_TO_SERVICE", "Value": "SrvBackMort"}]}, {"Id": "srv-n5gfxasy5jozp4jn", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-n5gfxasy5jozp4jn", "Name": "SrvBackMort", "Description": "Mortgage Backend", "DnsConfig": {}, "CreateDate": "2020-09-13 16:16:23.368000+02:00", "Instances": [{"Id": "InsBackMort", "Attributes": {"Name": "InsBackMort"}}], "ResponseMetadata": {"RequestId": "0ac519e2-996e-4029-a568-1b901b6a9410", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 14:17:13 GMT", "x-amzn-requestid": "0ac519e2-996e-4029-a568-1b901b6a9410", "content-length": "72", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": [{"Key": "NEO4J_RELATIONSHIP_TO_SERVICE", "Value": "ServiceMail"}]}, {"Id": "srv-xpdb5kwjlz4lflvy", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-xpdb5kwjlz4lflvy", "Name": "SrvBackInsu", "Description": "Insurance Backend", "DnsConfig": {}, "CreateDate": "2020-09-13 16:16:23.657000+02:00", "Instances": [{"Id": "InsBackInsu", "Attributes": {"Name": "InsBackInsu"}}], "ResponseMetadata": {"RequestId": "9079a064-1c4e-4125-9b1b-e2256553a8c7", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 14:17:13 GMT", "x-amzn-requestid": "9079a064-1c4e-4125-9b1b-e2256553a8c7", "content-length": "72", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": [{"Key": "NEO4J_RELATIONSHIP_TO_SERVICE", "Value": "ServiceMail"}]}, {"Id": "srv-ib7vejso77lmlsqb", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-ib7vejso77lmlsqb", "Name": "ServiceMail", "Description": "Mail", "DnsConfig": {}, "CreateDate": "2020-09-13 16:00:21.030000+02:00", "Instances": [{"Id": "InstanceMail", "Attributes": {"Name": "InstanceMail"}}], "ResponseMetadata": {"RequestId": "c5d89da0-6b7c-46e6-b13d-a09ace812cb6", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 14:17:13 GMT", "x-amzn-requestid": "c5d89da0-6b7c-46e6-b13d-a09ace812cb6", "content-length": "11", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": []}], "ResponseMetadata": {"RequestId": "15a920a7-e085-439d-a4cb-640cf8661242", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 14:17:12 GMT", "x-amzn-requestid": "15a920a7-e085-439d-a4cb-640cf8661242", "content-length": "1300", "connection": "keep-alive"}, "RetryAttempts": 0}}',
            'attributes': {
                    'ApproximateReceiveCount': '1',
                'SentTimestamp': '1600006634656',
                'SenderId': 'AIDAIW2MII4LNAEPTWTTO',
                'ApproximateFirstReceiveTimestamp': '1600006634657'
            },
            'messageAttributes': {},
            'md5OfBody': 'a57bed16a429157c1bf4c1c7092cbcab',
            'eventSource': 'aws:sqs',
            'eventSourceARN': 'arn:aws:sqs:eu-west-1:132017519464:marckqueue',
            'awsRegion': 'eu-west-1'
        }]
    }

    # full event 2

    # event = {
    #     'Records': [{
    #         'messageId': '18c3a17d-e7b9-4dac-be1e-7172a72a85a7',
    #         'receiptHandle': 'AQEBq5tSafEgXiUCEpZElkWrEliRbEYJx718ipu6FaGVXwRc0AUlDce3k4KhypWI1RKsrgTTUyldnZ2h3qvL0Cayh+MHC1oLfL7OXLvhQT4kKhtVsKVrM6FTboccxwzEPIhu28yP4RV+RCW2m7axXRcejsv/7QJDAIh/WdmYcEAm/gW5nVfjitYOcYF3UffTJpJOfmDy+fJmF5oBFc3tQC/IbLM3mMZY3n+rrPYl+ErZYgsc99wGOtKIpkubLCjRn27U4a/VQ9OPO0kj8qYe4/eCqJTwCEg+l9RHRix69RXkT8bFUg51K/Ps4VJxVGLZxlSleTIbyGAe1wghR0xnli7yW3d5DJtpC8gGSm0L1xXOtf8YE2T+Ld9Oyva+ctCC9Eig',
    #         'body': '{"Services": [{"Id": "srv-i45k3w2kvu4eis47", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-i45k3w2kvu4eis47", "Name": "service3", "DnsConfig": {}, "CreateDate": "2020-09-13 10:16:28.738000+02:00", "Instances": [{"Id": "instance3", "Attributes": {"name": "instance3"}}], "ResponseMetadata": {"RequestId": "8574eba7-0b4c-4e06-af90-c1c7cb2ecd52", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 08:26:03 GMT", "x-amzn-requestid": "8574eba7-0b4c-4e06-af90-c1c7cb2ecd52", "content-length": "11", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": []}, {"Id": "srv-lt7nd437bx7iuwkn", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-lt7nd437bx7iuwkn", "Name": "lambda-service", "Description": "Example Lambda service", "DnsConfig": {}, "CreateDate": "2020-06-07 12:47:41.489000+02:00", "Instances": [{"Id": "lambda-service_Instance", "Attributes": {"Arn": "arn:aws:lambda:eu-west-1:132017519464:function:lambda-service-ServiceFunction-KCJXDS03YTX"}}], "ResponseMetadata": {"RequestId": "006a9597-86a5-4ad6-adcd-89d5173ac625", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 08:26:04 GMT", "x-amzn-requestid": "006a9597-86a5-4ad6-adcd-89d5173ac625", "content-length": "11", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": []}, {"Id": "srv-4tyu3iu4hklxpcy2", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-4tyu3iu4hklxpcy2", "Name": "service4", "DnsConfig": {}, "CreateDate": "2020-09-13 10:16:37.822000+02:00", "Instances": [{"Id": "instance4", "Attributes": {"name": "instance4"}}], "ResponseMetadata": {"RequestId": "17ca7bf1-0a42-4f4c-8f7a-e7940bb98d0e", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 08:26:04 GMT", "x-amzn-requestid": "17ca7bf1-0a42-4f4c-8f7a-e7940bb98d0e", "content-length": "11", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": []}, {"Id": "srv-rms76dujd6v436rf", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-rms76dujd6v436rf", "Name": "service5", "DnsConfig": {}, "CreateDate": "2020-09-13 10:16:43.898000+02:00", "Instances": [{"Id": "instance5", "Attributes": {"name": "instance5"}}], "ResponseMetadata": {"RequestId": "91f0664b-b066-4064-9ca1-cc6b51b3f855", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 08:26:05 GMT", "x-amzn-requestid": "91f0664b-b066-4064-9ca1-cc6b51b3f855", "content-length": "11", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": []}, {"Id": "srv-ikl7elv7qwtf55yf", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-ikl7elv7qwtf55yf", "Name": "backend1", "DnsConfig": {}, "CreateDate": "2020-09-13 10:15:53.538000+02:00", "Instances": [{"Id": "instance_backend1", "Attributes": {"name": "instance_backend1"}}], "ResponseMetadata": {"RequestId": "9ae16d54-5b39-4ed8-a8ed-768ec7356f9c", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 08:26:04 GMT", "x-amzn-requestid": "9ae16d54-5b39-4ed8-a8ed-768ec7356f9c", "content-length": "11", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": []}, {"Id": "srv-34vo24xaz5of7pfi", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-34vo24xaz5of7pfi", "Name": "frontend1", "DnsConfig": {}, "CreateDate": "2020-09-13 10:15:31.940000+02:00", "Instances": [{"Id": "instance_frontend1", "Attributes": {"name": "instance_frontend1"}}], "ResponseMetadata": {"RequestId": "68fbbf09-841b-4f50-8c65-4208208706ea", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 08:26:04 GMT", "x-amzn-requestid": "68fbbf09-841b-4f50-8c65-4208208706ea", "content-length": "69", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": [{"Key": "NEO4J_RELATIONSHIP_TO_SERVICE", "Value": "backend1"}]}, {"Id": "srv-wq7zxkmpkgvj6wu2", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-wq7zxkmpkgvj6wu2", "Name": "backend2", "DnsConfig": {}, "CreateDate": "2020-09-13 10:16:12.362000+02:00", "Instances": [{"Id": "instance_backend2", "Attributes": {"name": "instance_backend2"}}], "ResponseMetadata": {"RequestId": "e3f18f4e-f955-49d7-9a23-6f016f3aca91", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 08:26:04 GMT", "x-amzn-requestid": "e3f18f4e-f955-49d7-9a23-6f016f3aca91", "content-length": "11", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": []}, {"Id": "srv-vjkyavnpvvtyps3f", "Arn": "arn:aws:servicediscovery:eu-west-1:132017519464:service/srv-vjkyavnpvvtyps3f", "Name": "frontend2", "DnsConfig": {}, "CreateDate": "2020-09-13 10:16:01.904000+02:00", "Instances": [{"Id": "instance_frontend2", "Attributes": {"name": "instance_frontend2"}}], "ResponseMetadata": {"RequestId": "131d2fce-be2b-4c8d-ad9b-07554eb20769", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 08:26:04 GMT", "x-amzn-requestid": "131d2fce-be2b-4c8d-ad9b-07554eb20769", "content-length": "69", "connection": "keep-alive"}, "RetryAttempts": 0}, "Tags": [{"Key": "NEO4J_RELATIONSHIP_TO_SERVICE", "Value": "backend2"}]}], "ResponseMetadata": {"RequestId": "46c397d4-7e37-4e0a-b167-bd7400292275", "HTTPStatusCode": 200, "HTTPHeaders": {"content-type": "application/x-amz-json-1.1", "date": "Sun, 13 Sep 2020 08:26:04 GMT", "x-amzn-requestid": "46c397d4-7e37-4e0a-b167-bd7400292275", "content-length": "1484", "connection": "keep-alive"}, "RetryAttempts": 0}}',
    #         'attributes': {
    #                 'ApproximateReceiveCount': '1',
    #             'SentTimestamp': '1599985566018',
    #             'SenderId': 'AIDAIW2MII4LNAEPTWTTO',
    #             'ApproximateFirstReceiveTimestamp': '1599985566032'
    #         },
    #         'messageAttributes': {},
    #         'md5OfBody': 'd2361fe82e3611afa659059a140edeb0',
    #         'eventSource': 'aws:sqs',
    #         'eventSourceARN': 'arn:aws:sqs:eu-west-1:132017519464:marckqueue',
    #         'awsRegion': 'eu-west-1'
    #     }]
    # }

    lambda_handler(event, "")
