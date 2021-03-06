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
