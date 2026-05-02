import * as cdk from "aws-cdk-lib";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import { WebSocketLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda_ from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
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
      code: lambda_.Code.fromInline(`
import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import urllib.request

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

def _b64url_decode(s):
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)

def _verify_town_token(token, secret):
    """Verify HMAC-signed town token. Returns payload dict or None.

    Tokens with an \`\`exp\`\` in the past are rejected. Tokens missing \`\`exp\`\`
    are accepted for backward compatibility (logged at WARNING) — must stay
    in sync with backend \`\`core/town_token.py:verify_town_token\`\`.
    """
    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts
    try:
        expected_sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected_sig, actual_sig):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
        if "uid" not in payload or "iid" not in payload:
            return None
    except Exception:
        return None
    exp = payload.get("exp")
    if exp is None:
        logger.warning("legacy town token used, no exp (uid=%s iid=%s)", payload.get("uid"), payload.get("iid"))
    else:
        import time as _time
        try:
            if int(exp) < int(_time.time()):
                return None
        except (TypeError, ValueError):
            return None
    return payload

def _b64url_to_int(s):
    """Decode a base64url-encoded big-endian unsigned integer."""
    data = _b64url_decode(s)
    return int.from_bytes(data, "big")

def _verify_rs256(message_bytes, signature_bytes, n_int, e_int):
    """Verify RS256 signature using raw math (no external crypto libs)."""
    sig_int = int.from_bytes(signature_bytes, "big")
    key_len = (n_int.bit_length() + 7) // 8
    if len(signature_bytes) != key_len:
        return False
    decrypted = pow(sig_int, e_int, n_int)
    em = decrypted.to_bytes(key_len, "big")
    # PKCS#1 v1.5: 0x00 0x01 [padding 0xFF] 0x00 [DigestInfo]
    # SHA-256 DigestInfo prefix
    sha256_prefix = bytes.fromhex(
        "3031300d060960864801650304020105000420"
    )
    digest = hashlib.sha256(message_bytes).digest()
    expected_suffix = sha256_prefix + digest
    # em should be: 00 01 FF...FF 00 <expected_suffix>
    if len(em) < len(expected_suffix) + 11:
        return False
    if em[0] != 0 or em[1] != 1:
        return False
    suffix_start = len(em) - len(expected_suffix)
    if em[suffix_start - 1] != 0:
        return False
    if em[suffix_start:] != expected_suffix:
        return False
    # Check padding bytes are all 0xFF
    for i in range(2, suffix_start - 1):
        if em[i] != 0xFF:
            return False
    return True

_jwks_cache = {}

def _get_jwks(jwks_url):
    if jwks_url in _jwks_cache:
        return _jwks_cache[jwks_url]
    req = urllib.request.Request(jwks_url, headers={"User-Agent": "GooseTown-Authorizer/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    _jwks_cache[jwks_url] = data
    return data

def _verify_clerk_jwt(token, clerk_issuer):
    """Verify a Clerk JWT using JWKS. Returns subject or None."""
    import time as _time

    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except Exception:
        return None

    if header.get("alg") != "RS256":
        logger.warning("Unsupported JWT algorithm: %s", header.get("alg"))
        return None

    kid = header.get("kid")
    if not kid:
        return None

    # Verify expiry
    now = int(_time.time())
    if payload.get("exp", 0) < now:
        logger.warning("JWT expired")
        return None

    # Verify issuer
    if payload.get("iss") != clerk_issuer:
        logger.warning("JWT issuer mismatch: %s != %s", payload.get("iss"), clerk_issuer)
        return None

    # Fetch JWKS and find key
    jwks_url = clerk_issuer.rstrip("/") + "/.well-known/jwks.json"
    try:
        jwks = _get_jwks(jwks_url)
    except Exception as e:
        logger.error("Failed to fetch JWKS: %s", e)
        return None

    rsa_key = None
    for key in jwks.get("keys", []):
        if key.get("kid") == kid and key.get("kty") == "RSA":
            rsa_key = key
            break
    if not rsa_key:
        logger.warning("No matching RSA key found for kid=%s", kid)
        return None

    # Verify signature
    n_int = _b64url_to_int(rsa_key["n"])
    e_int = _b64url_to_int(rsa_key["e"])
    message = (header_b64 + "." + payload_b64).encode("ascii")
    if not _verify_rs256(message, signature, n_int, e_int):
        logger.warning("JWT signature verification failed")
        return None

    return payload.get("sub")

def _extract_token(event):
    # Prefer "Authorization: Bearer <token>" header (kept out of access logs).
    # Fall back to the legacy ?token= query param so daemons that still ship
    # the older skill bundle keep working through the transition.
    headers = event.get("headers") or {}
    for k, v in headers.items():
        if k.lower() == "authorization" and isinstance(v, str):
            if v.lower().startswith("bearer "):
                return v[7:].strip(), "header"
            return v.strip(), "header"
    qs = event.get("queryStringParameters") or {}
    legacy_token = qs.get("token") or ""
    if legacy_token:
        return legacy_token, "query"
    return "", "none"

def handler(event, context):
    method_arn = event.get("methodArn", "")
    # Don't log the full event — it contains the token in queryStringParameters
    # or headers depending on the auth path. Log only the route + source.
    route_key = event.get("requestContext", {}).get("routeKey", "")
    token, source = _extract_token(event)
    logger.info("Authorizer route=%s token_source=%s", route_key, source)

    if not token:
        logger.warning("No token provided")
        return generate_policy("anonymous", "Deny", method_arn)

    # Try town token first (HMAC-signed)
    try:
        town_secret = _get_secret(os.environ["TOWN_TOKEN_SECRET"])
        payload = _verify_town_token(token, town_secret)
        if payload:
            user_id = payload.get("uid", "town-agent")
            logger.info("Town token verified for user %s", user_id)
            return generate_policy(user_id, "Allow", method_arn)
    except Exception as e:
        logger.warning("Town token check failed: %s", e)

    # Try Clerk JWT
    try:
        clerk_issuer = _get_secret(os.environ["CLERK_ISSUER"])
        subject = _verify_clerk_jwt(token, clerk_issuer)
        if subject:
            logger.info("Clerk JWT verified for user %s", subject)
            return generate_policy(subject, "Allow", method_arn)
    except Exception as e:
        logger.warning("Clerk JWT check failed: %s", e)

    logger.warning("All token verification methods failed")
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
      timeout: cdk.Duration.seconds(15),
      memorySize: 128,
      logGroup: disconnectLogGroup,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      environment: {
        CONNECTIONS_TABLE: props.connectionsTable.tableName,
        BACKEND_URL:
          props.backendUrl ?? `http://api-${env}.goosetown.isol8.co`,
      },
      code: lambda_.Code.fromInline(`
import json
import logging
import os
import urllib.request
import urllib.error

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["CONNECTIONS_TABLE"])
BACKEND_URL = os.environ.get("BACKEND_URL", "").rstrip("/")

def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    logger.info("Disconnect: %s", connection_id)

    table.delete_item(Key={"connectionId": connection_id})

    # Notify backend to clean up agent from game server and DynamoDB
    if BACKEND_URL:
        payload = json.dumps({"connectionId": connection_id}).encode("utf-8")
        url = f"{BACKEND_URL}/api/v1/town/ws-disconnect"
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                logger.info("Backend cleanup: %s", resp.status)
        except Exception as e:
            logger.warning("Backend cleanup failed: %s", e)

    return {"statusCode": 200, "body": "Disconnected"}
`),
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

    const defaultFn = new lambda_.Function(this, "DefaultRouteFunction", {
      functionName: `goosetown-${env}-ws-default`,
      runtime: lambda_.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.seconds(15),
      memorySize: 128,
      logGroup: defaultLogGroup,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      environment: {
        BACKEND_URL: backendUrl,
      },
      code: lambda_.Code.fromInline(`
import json
import logging
import os
import urllib.request
import urllib.error

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BACKEND_URL = os.environ["BACKEND_URL"].rstrip("/")

def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    authorizer_ctx = event.get("requestContext", {}).get("authorizer", {})
    principal_id = authorizer_ctx.get("principalId", "unknown")
    body = event.get("body", "")

    logger.info("Default route: connection=%s principal=%s body=%s", connection_id, principal_id, body[:200])

    payload = json.dumps({
        "connectionId": connection_id,
        "principalId": principal_id,
        "body": json.loads(body) if body else {},
    }).encode("utf-8")

    url = f"{BACKEND_URL}/api/v1/town/ws-message"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = resp.read().decode("utf-8")
            logger.info("Backend responded: %s %s", resp.status, resp_body[:200])
        return {"statusCode": 200}
    except urllib.error.HTTPError as e:
        logger.error("Backend HTTP error: %s %s", e.code, e.read().decode("utf-8")[:500])
        return {"statusCode": 500}
    except Exception as e:
        logger.error("Backend request failed: %s", e)
        return {"statusCode": 500}
`),
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
