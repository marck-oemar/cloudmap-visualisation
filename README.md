# Visualisation of AWS Cloudmap services with NEO4J

![image](images/neo4j_example.png)


## Context
[*AWS Cloud Map Service Discovery*](https://aws.amazon.com/cloud-map/) is a great way to implement service discovery and to have an overview of live services and instances.
This PoC takes CloudMap a step further and aims to visualize the services and the dependencies between them by using [*Neo4j*](https://neo4j.com).

## Neo4j
Neo4j is a NOSQL graph database that allows us to easily visualize data and relations between data. 
It can be queried through the [*Cypher query language*](https://neo4j.com/developer/cypher/).
Once the data is in the neo4j graph database, we can query and visualize the database.

## Approach
Instead of processing each change in Cloudmap and try to reflect it in the Neo4j database, we take a 'snapshot' of the whole Cloudmap configuration, update the Neo4j database and remove obsolete services, instances and relations. 

The flow is as follows:
CloudMap -> Producer -> SQSQueue -> Consumer (Neomodel)-> Neo4j database

- The Producer creates a payload of all services and instances and put's it in a message on the Queue.
- The Consumer processes 1 message at a time and uses the message id to mark each node in neo4j when it updates or creates nodes. To make sure this transaction is atomic, we've implemented single concurrency for the Neo4j database, that is, only 1 transaction can be executed at a time. This is done through a locking mechanism with DynamoDB. 
Finally the Consumer removes nodes that does not have the current message id, effectively matching the configuration of Cloudmap.
- To interact with the Neo4j database we use [*Neomodel*](https://neomodel.readthedocs.io/en/latest/)

## Technology
- [*AWS Cloud Map Service Discovery*](https://aws.amazon.com/cloud-map/)
- Python
- [*AWS Lambda*](https://aws.amazon.com/lambda/)
- [*AWS SQS*](https://aws.amazon.com/sqs/)
- [*AWS DynamoDB*](https://aws.amazon.com/dynamodb/)
- [*Neomodel*](https://neomodel.readthedocs.io/en/latest/), a python Object Graph Mapper (OGM) for the neo4j graph database
- [*Neo4j 3.5 container image*](https://hub.docker.com/_/neo4j), to store and visualize data
- [*Cypher query language*](https://neo4j.com/developer/cypher/)
- [*AWS SAM*](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/what-is-sam.html)


## Data structure
The data structure in CloudMap that we're trying to model in Neo4j is quite straight forward:
- Services
- Each Service can have Instance(s) registered

The only customization is a relationship between services, for instance a front-end that depends on a back-end.
To visualize this relationship, we have defined a special tag on Service "NEO4J_RELATIONSHIP_TO_SERVICE".

This repo includes a [dummy example of services and instances](cloudmap-example/template.yaml):
- Mortgage application with front-end and back-end
- Insurance application with front-end and back-end
- Mail service that is a dependency for the back-end services

## Missing features
### Automatic trigger between Cloudmap and the Producer
The Producer Lambda function must be invoked manually, because the Cloudwatch rule is not implemented yet.  

### Neo4j
The repository does not have a Neo4j service yet, you'll have to run it yourself.

## Requirements
- AWS account
- AWS CLI
- SAM CLI
- Docker Engine
- Container service that can run a container and expose it publicly

## How to install
In the following order:

### 1 - CloudMap
- Create a CloudMap API namespace
- Use the [*cloudmap-example SAM template*](cloudmap-example/template.yaml) in this repo to deploy example services and instances
- If you want to have a relationship between services, create a tag 'NEO4J_RELATIONSHIP_TO_SERVICE' on the service with the name of the other service as the value


### 2 - Neo4j database
- Run a [neo4j:3.5 container](https://hub.docker.com/_/neo4j) publicly, for example on Kubernetes or AWS ECS. Note the public ip address / dns name.
- Expose the Neo4j TCP ports (7474 and 7687) for HTTP and Bolt access)
- Go to the HTTP interface. You will be asked to reset the password
- In the Cypher query interface, create a constraint on 'Service' node:
```
CREATE CONSTRAINT ON (n:Service) ASSERT n.name IS UNIQUE
```


### 3 - Deploy the stack
Deploy the SAM template which will deploy:
- Producer lambda function
- SQS queue
- Consumer lambda function
- DynamoDB table 


```
sam build -u
sam deploy --guided
```

You'll be asked for the following parameters:
- NamespaceId. The namespace id (not name) of the CloudMap Namespace.
- Neo4jUrl. The Neo4j database url which has naming convention:
```
bolt://<username>:<password>@<neo4j ip address or dns name>:7687
```


## How to use
Manually invoke the Producer lambda function. This will put a message with a Cloudmap content payload on the queue.
The Consumer will pick up the message and execute a transaction against the Neo4j database.
To see the visualisation simply use the Neo4j Cypher query:
```
MATCH (n) RETURN n  
```


## Useful cypher queries
```
# generate graph
MATCH (n) RETURN n   

# delete all 
MATCH (n) DETACH DELETE n

# delete single node
MATCH (n:Resource { name: 'service-example' }) DELETE n

# unique
CREATE CONSTRAINT ON (n:Service) ASSERT n.name IS UNIQUE

```
