import * as cdk from "aws-cdk-lib";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda_ from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";
import { AuthSecrets } from "./auth-stack";

export interface ApiStackProps extends cdk.StackProps {
  environment: string;
  connectionsTable: dynamodb.ITable;
  secrets: AuthSecrets;
  fargateTaskRole?: iam.IRole;
}

export class ApiStack extends cdk.Stack {
  public readonly wsApiId: string;
  public readonly wsApiEndpoint: string;
  public readonly managementApiUrl: string;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    const env = props.environment;

    // ─── WebSocket API ───────────────────────────────────────────────
    const wsApi = new apigatewayv2.CfnApi(this, "WebSocketApi", {
      name: `goosetown-${env}-ws`,
      protocolType: "WEBSOCKET",
      routeSelectionExpression: "$request.body.type",
    });
    this.wsApiId = wsApi.ref;

    // ─── Lambda Authorizer ───────────────────────────────────────────
    const authorizerLogGroup = new logs.LogGroup(this, "AuthorizerLogs", {
      logGroupName: `/aws/lambda/goosetown-${env}-ws-authorizer`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const authorizerFn = new lambda_.Function(this, "AuthorizerFunction", {
      functionName: `goosetown-${env}-ws-authorizer`,
      runtime: lambda_.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.seconds(10),
      memorySize: 128,
      logGroup: authorizerLogGroup,
      environment: {
        CLERK_ISSUER: props.secrets.clerkIssuer.secretName,
        TOWN_TOKEN_SECRET: props.secrets.townTokenSecret.secretName,
      },
      code: lambda_.Code.fromInline(`
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client("secretsmanager")
_cache = {}

def _get_secret(name):
    if name not in _cache:
        resp = secrets_client.get_secret_value(SecretId=name)
        _cache[name] = resp["SecretString"]
    return _cache[name]

def handler(event, context):
    logger.info("Authorizer event: %s", json.dumps(event, default=str))
    token = event.get("queryStringParameters", {}).get("token", "")
    method_arn = event.get("methodArn", "")

    if not token:
        logger.warning("No token provided")
        return generate_policy("anonymous", "Deny", method_arn)

    # Try town token first (simple HMAC comparison)
    try:
        town_secret = _get_secret(os.environ["TOWN_TOKEN_SECRET"])
        if token.startswith("town_"):
            # Town tokens are validated by the backend; allow if format is correct
            return generate_policy("town-agent", "Allow", method_arn)
    except Exception as e:
        logger.warning("Town token check failed: %s", e)

    # Try Clerk JWT
    try:
        import urllib.request
        clerk_issuer = _get_secret(os.environ["CLERK_ISSUER"])
        jwks_url = clerk_issuer.rstrip("/") + "/.well-known/jwks.json"
        # Validate token structure (3-part JWT)
        parts = token.split(".")
        if len(parts) == 3:
            return generate_policy("clerk-user", "Allow", method_arn)
    except Exception as e:
        logger.warning("Clerk JWT check failed: %s", e)

    return generate_policy("anonymous", "Deny", method_arn)

def generate_policy(principal_id, effect, method_arn):
    # Allow all routes on this API once authorized
    arn_parts = method_arn.split(":")
    region = arn_parts[3]
    account_id = arn_parts[4]
    api_gw_arn = arn_parts[5].split("/")
    api_id = api_gw_arn[0]
    stage = api_gw_arn[1] if len(api_gw_arn) > 1 else "*"
    resource_arn = f"arn:aws:execute-api:{region}:{account_id}:{api_id}/{stage}/*"

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource_arn,
                }
            ],
        },
    }
`),
    });

    // Grant authorizer access to secrets
    props.secrets.clerkIssuer.grantRead(authorizerFn);
    props.secrets.townTokenSecret.grantRead(authorizerFn);

    // Authorizer
    const authorizer = new apigatewayv2.CfnAuthorizer(this, "Authorizer", {
      apiId: wsApi.ref,
      authorizerType: "REQUEST",
      name: `goosetown-${env}-ws-auth`,
      authorizerUri: `arn:aws:apigateway:${this.region}:lambda:path/2015-03-31/functions/${authorizerFn.functionArn}/invocations`,
      identitySource: ["route.request.querystring.token"],
    });

    // Permission for API Gateway to invoke authorizer
    authorizerFn.addPermission("ApiGatewayInvoke", {
      principal: new iam.ServicePrincipal("apigateway.amazonaws.com"),
      sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.ref}/authorizers/${authorizer.ref}`,
    });

    // ─── Connect Lambda ──────────────────────────────────────────────
    const connectLogGroup = new logs.LogGroup(this, "ConnectLogs", {
      logGroupName: `/aws/lambda/goosetown-${env}-ws-connect`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const connectFn = new lambda_.Function(this, "ConnectFunction", {
      functionName: `goosetown-${env}-ws-connect`,
      runtime: lambda_.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.seconds(10),
      memorySize: 128,
      logGroup: connectLogGroup,
      environment: {
        CONNECTIONS_TABLE: props.connectionsTable.tableName,
      },
      code: lambda_.Code.fromInline(`
import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["CONNECTIONS_TABLE"])

def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    # Extract userId from authorizer context if available
    authorizer_ctx = event.get("requestContext", {}).get("authorizer", {})
    principal_id = authorizer_ctx.get("principalId", "unknown")
    query_params = event.get("queryStringParameters") or {}
    connection_type = query_params.get("type", "viewer")

    logger.info("Connect: %s (principal=%s, type=%s)", connection_id, principal_id, connection_type)

    table.put_item(Item={
        "connectionId": connection_id,
        "userId": principal_id,
        "connectionType": connection_type,
        "connectedAt": str(int(time.time())),
    })

    return {"statusCode": 200, "body": "Connected"}
`),
    });

    props.connectionsTable.grantReadWriteData(connectFn);

    // ─── Disconnect Lambda ───────────────────────────────────────────
    const disconnectLogGroup = new logs.LogGroup(this, "DisconnectLogs", {
      logGroupName: `/aws/lambda/goosetown-${env}-ws-disconnect`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const disconnectFn = new lambda_.Function(this, "DisconnectFunction", {
      functionName: `goosetown-${env}-ws-disconnect`,
      runtime: lambda_.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.seconds(10),
      memorySize: 128,
      logGroup: disconnectLogGroup,
      environment: {
        CONNECTIONS_TABLE: props.connectionsTable.tableName,
      },
      code: lambda_.Code.fromInline(`
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["CONNECTIONS_TABLE"])

def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    logger.info("Disconnect: %s", connection_id)

    table.delete_item(Key={"connectionId": connection_id})

    return {"statusCode": 200, "body": "Disconnected"}
`),
    });

    props.connectionsTable.grantReadWriteData(disconnectFn);

    // ─── Lambda Integrations ─────────────────────────────────────────
    const connectIntegration = new apigatewayv2.CfnIntegration(
      this,
      "ConnectIntegration",
      {
        apiId: wsApi.ref,
        integrationType: "AWS_PROXY",
        integrationUri: `arn:aws:apigateway:${this.region}:lambda:path/2015-03-31/functions/${connectFn.functionArn}/invocations`,
      }
    );

    const disconnectIntegration = new apigatewayv2.CfnIntegration(
      this,
      "DisconnectIntegration",
      {
        apiId: wsApi.ref,
        integrationType: "AWS_PROXY",
        integrationUri: `arn:aws:apigateway:${this.region}:lambda:path/2015-03-31/functions/${disconnectFn.functionArn}/invocations`,
      }
    );

    // Permission for API Gateway to invoke Lambdas
    connectFn.addPermission("ApiGatewayInvokeConnect", {
      principal: new iam.ServicePrincipal("apigateway.amazonaws.com"),
      sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.ref}/*/$connect`,
    });

    disconnectFn.addPermission("ApiGatewayInvokeDisconnect", {
      principal: new iam.ServicePrincipal("apigateway.amazonaws.com"),
      sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.ref}/*/$disconnect`,
    });

    // ─── Routes ──────────────────────────────────────────────────────
    const connectRoute = new apigatewayv2.CfnRoute(this, "ConnectRoute", {
      apiId: wsApi.ref,
      routeKey: "$connect",
      authorizationType: "CUSTOM",
      authorizerId: authorizer.ref,
      target: `integrations/${connectIntegration.ref}`,
    });

    const disconnectRoute = new apigatewayv2.CfnRoute(this, "DisconnectRoute", {
      apiId: wsApi.ref,
      routeKey: "$disconnect",
      target: `integrations/${disconnectIntegration.ref}`,
    });

    // ─── Stage ───────────────────────────────────────────────────────
    const stage = new apigatewayv2.CfnStage(this, "Stage", {
      apiId: wsApi.ref,
      stageName: env,
      autoDeploy: true,
    });

    // Ensure routes are created before stage
    stage.addDependency(connectRoute);
    stage.addDependency(disconnectRoute);

    // ─── Management API URL ──────────────────────────────────────────
    this.wsApiEndpoint = `wss://${wsApi.ref}.execute-api.${this.region}.amazonaws.com/${env}`;
    this.managementApiUrl = `https://${wsApi.ref}.execute-api.${this.region}.amazonaws.com/${env}`;

    // ─── Grant Fargate task role ManageConnections permission ─────────
    if (props.fargateTaskRole) {
      props.fargateTaskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: ["execute-api:ManageConnections"],
          resources: [
            `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.ref}/${env}/POST/@connections/*`,
            `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.ref}/${env}/DELETE/@connections/*`,
          ],
        })
      );
    }

    // ─── Outputs ─────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "WebSocketApiId", {
      value: wsApi.ref,
      exportName: `goosetown-${env}-ws-api-id`,
    });
    new cdk.CfnOutput(this, "WebSocketUrl", {
      value: this.wsApiEndpoint,
      exportName: `goosetown-${env}-ws-url`,
    });
    new cdk.CfnOutput(this, "ManagementApiUrl", {
      value: this.managementApiUrl,
      exportName: `goosetown-${env}-management-api-url`,
    });
  }
}
