import * as cdk from "aws-cdk-lib";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import { WebSocketLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda_ from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as path from "path";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53Targets from "aws-cdk-lib/aws-route53-targets";
import { Construct } from "constructs";
import { AuthSecrets } from "./auth-stack";

export interface ApiStackProps extends cdk.StackProps {
  environment: string;
  connectionsTable: dynamodb.ITable;
  secrets: AuthSecrets;
  fargateTaskRole?: iam.IRole;
  vpc: ec2.IVpc;
  backendUrl?: string;
  certificate?: acm.ICertificate;
  hostedZone?: route53.IHostedZone;
}

export class ApiStack extends cdk.Stack {
  public readonly wsApiId: string;
  public readonly wsApiEndpoint: string;
  public readonly managementApiUrl: string;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    const env = props.environment;

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
      timeout: cdk.Duration.seconds(15),
      memorySize: 128,
      logGroup: authorizerLogGroup,
      environment: {
        CLERK_ISSUER: props.secrets.clerkIssuer.secretName,
        TOWN_TOKEN_SECRET: props.secrets.townTokenSecret.secretName,
      },
      // Lambda code is in apps/infra/lambda/ws-authorizer/index.py.
      // Code.fromAsset bundles the directory as a ZIP asset; CDK treats this
      // as a CODE update (not a function replacement) so there is no ENI/VPC
      // disruption on deploy.
      code: lambda_.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "ws-authorizer")
      ),
    });

    // Grant authorizer access to secrets
    props.secrets.clerkIssuer.grantRead(authorizerFn);
    props.secrets.townTokenSecret.grantRead(authorizerFn);

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
      timeout: cdk.Duration.seconds(15),
      memorySize: 128,
      logGroup: connectLogGroup,
      environment: {
        CONNECTIONS_TABLE: props.connectionsTable.tableName,
      },
      // Lambda code is in apps/infra/lambda/ws-connect/index.py.
      code: lambda_.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "ws-connect")
      ),
    });

    props.connectionsTable.grantReadWriteData(connectFn);

    // ─── Disconnect Lambda ───────────────────────────────────────────
    const disconnectLogGroup = new logs.LogGroup(this, "DisconnectLogs", {
      logGroupName: `/aws/lambda/goosetown-${env}-ws-disconnect`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // No VPC config — this Lambda only calls public endpoints
    // (DynamoDB, the public ALB at api-{env}.goosetown.isol8.co,
    // CloudWatch Logs). Putting it in a VPC was forcing AWS to provision
    // Lambda VPC ENIs, which detach asynchronously (20-45 min) on every
    // immutable property change. CloudFormation tried to delete the
    // function's SG before the ENIs cleared, leaving DELETE_FAILED
    // tombstones that blocked subsequent deploys (the chronic prod-api
    // failure mode through 2026-05-02). IAM is the security boundary
    // for Lambda, not VPC placement; placing the function in a VPC adds
    // no defense-in-depth when its only callees are public AWS services.
    const disconnectFn = new lambda_.Function(this, "DisconnectFunction", {
      functionName: `goosetown-${env}-ws-disconnect`,
      runtime: lambda_.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.seconds(15),
      memorySize: 128,
      logGroup: disconnectLogGroup,
      environment: {
        CONNECTIONS_TABLE: props.connectionsTable.tableName,
        BACKEND_URL:
          props.backendUrl ?? `http://api-${env}.goosetown.isol8.co`,
      },
      // Lambda code is in apps/infra/lambda/ws-disconnect/index.py.
      code: lambda_.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "ws-disconnect")
      ),
    });

    props.connectionsTable.grantReadWriteData(disconnectFn);

    // ─── Default Route Lambda (forwards WS messages to backend ALB) ──
    const defaultLogGroup = new logs.LogGroup(this, "DefaultRouteLogs", {
      logGroupName: `/aws/lambda/goosetown-${env}-ws-default`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const backendUrl =
      props.backendUrl ?? `http://api-${env}.goosetown.isol8.co`;

    // No VPC config — see DisconnectFunction above for the same rationale.
    const defaultFn = new lambda_.Function(this, "DefaultRouteFunction", {
      functionName: `goosetown-${env}-ws-default`,
      runtime: lambda_.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.seconds(15),
      memorySize: 128,
      logGroup: defaultLogGroup,
      environment: {
        BACKEND_URL: backendUrl,
      },
      // Lambda code is in apps/infra/lambda/ws-default/index.py.
      code: lambda_.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "ws-default")
      ),
    });

    // ─── L2 WebSocket API ────────────────────────────────────────────
    // The L2 WebSocketApi does not natively support REQUEST authorizers
    // on connectRouteOptions, so we create the API with disconnect/default
    // routes via L2 and attach the $connect route + authorizer via L1.

    const disconnectIntegration = new WebSocketLambdaIntegration(
      "DisconnectInteg",
      disconnectFn
    );
    const defaultIntegration = new WebSocketLambdaIntegration(
      "DefaultInteg",
      defaultFn
    );

    const wsApi = new apigatewayv2.WebSocketApi(this, "WebSocketApi", {
      apiName: `goosetown-${env}-ws`,
      routeSelectionExpression: "$request.body.type",
      disconnectRouteOptions: {
        integration: disconnectIntegration,
      },
      defaultRouteOptions: {
        integration: defaultIntegration,
      },
    });

    this.wsApiId = wsApi.apiId;

    // ─── $connect route with authorizer (L1 — L2 lacks authorizer support) ─
    // We use L1 CfnIntegration + CfnRoute for $connect because the L2
    // WebSocketApi doesn't support REQUEST authorizers on connectRouteOptions.
    const connectIntegration = new apigatewayv2.CfnIntegration(
      this,
      "ConnectIntegration",
      {
        apiId: wsApi.apiId,
        integrationType: "AWS_PROXY",
        integrationUri: `arn:aws:apigateway:${this.region}:lambda:path/2015-03-31/functions/${connectFn.functionArn}/invocations`,
      }
    );

    const authorizer = new apigatewayv2.CfnAuthorizer(this, "Authorizer", {
      apiId: wsApi.apiId,
      authorizerType: "REQUEST",
      name: `goosetown-${env}-ws-auth`,
      authorizerUri: `arn:aws:apigateway:${this.region}:lambda:path/2015-03-31/functions/${authorizerFn.functionArn}/invocations`,
      identitySource: ["route.request.querystring.token"],
    });

    // Permission for API Gateway to invoke authorizer
    authorizerFn.addPermission("ApiGatewayInvoke", {
      principal: new iam.ServicePrincipal("apigateway.amazonaws.com"),
      sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.apiId}/authorizers/${authorizer.ref}`,
    });

    const connectRoute = new apigatewayv2.CfnRoute(this, "ConnectRoute", {
      apiId: wsApi.apiId,
      routeKey: "$connect",
      authorizationType: "CUSTOM",
      authorizerId: authorizer.ref,
      target: `integrations/${connectIntegration.ref}`,
    });

    // Permission for API Gateway to invoke connect Lambda
    connectFn.addPermission("ApiGatewayInvokeConnect", {
      principal: new iam.ServicePrincipal("apigateway.amazonaws.com"),
      sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.apiId}/*/$connect`,
    });

    // ─── Stage ───────────────────────────────────────────────────────
    const stage = new apigatewayv2.WebSocketStage(this, "Stage", {
      webSocketApi: wsApi,
      stageName: env,
      autoDeploy: true,
    });

    // ─── Management API URL ──────────────────────────────────────────
    this.wsApiEndpoint = `wss://${wsApi.apiId}.execute-api.${this.region}.amazonaws.com/${env}`;
    this.managementApiUrl = `https://${wsApi.apiId}.execute-api.${this.region}.amazonaws.com/${env}`;

    // ─── Grant Fargate task role ManageConnections permission ─────────
    if (props.fargateTaskRole) {
      props.fargateTaskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: ["execute-api:ManageConnections"],
          resources: [
            `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.apiId}/${env}/POST/@connections/*`,
            `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.apiId}/${env}/DELETE/@connections/*`,
          ],
        })
      );
    }

    // ─── Custom Domain (ws-{env}.goosetown.isol8.co) ─────────────────
    if (props.certificate && props.hostedZone) {
      const domainName = new apigatewayv2.DomainName(this, "WsDomain", {
        domainName: `ws-${env}.goosetown.isol8.co`,
        certificate: props.certificate,
      });

      new apigatewayv2.ApiMapping(this, "WsApiMapping", {
        api: wsApi,
        domainName,
        stage,
      });

      new route53.ARecord(this, "WsDnsRecord", {
        zone: props.hostedZone,
        recordName: `ws-${env}.goosetown.isol8.co`,
        target: route53.RecordTarget.fromAlias(
          new route53Targets.ApiGatewayv2DomainProperties(
            domainName.regionalDomainName,
            domainName.regionalHostedZoneId,
          )
        ),
      });
    }

    // ─── Outputs ─────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "WebSocketApiId", {
      value: wsApi.apiId,
      exportName: `goosetown-${env}-ws-api-id`,
    });
    new cdk.CfnOutput(this, "WebSocketUrl", {
      value: this.wsApiEndpoint,
      exportName: `goosetown-${env}-ws-url`,
    });
    // TODO(sub-project-3): remove this dangling CfnOutput once compute-stack has deployed without
    // importValue. Left in place for one deploy cycle so an emergency rollback to an older
    // ComputeStack revision (which still uses Fn.importValue) can resolve the export.
    new cdk.CfnOutput(this, "ManagementApiUrl", {
      value: this.managementApiUrl,
      exportName: `goosetown-${env}-management-api-url`,
    });
  }
}
