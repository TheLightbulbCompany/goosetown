import * as cdk from "aws-cdk-lib";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

export interface AuthStackProps extends cdk.StackProps {
  environment: string;
}

export interface AuthSecrets {
  clerkIssuer: secretsmanager.ISecret;
  clerkSecretKey: secretsmanager.ISecret;
  townTokenSecret: secretsmanager.ISecret;
  pixelLabApiKey: secretsmanager.ISecret;
  resendApiKey: secretsmanager.ISecret;
  spriteEventSecret: secretsmanager.ISecret;
  wsLambdaSecret: secretsmanager.ISecret;
}

export class AuthStack extends cdk.Stack {
  public readonly secrets: AuthSecrets;

  constructor(scope: Construct, id: string, props: AuthStackProps) {
    super(scope, id, props);

    const env = props.environment;

    const clerkIssuer = new secretsmanager.Secret(this, "ClerkIssuer", {
      secretName: `goosetown/${env}/clerk_issuer`,
      description: "Clerk JWT issuer URL for GooseTown",
    });

    const clerkSecretKey = new secretsmanager.Secret(this, "ClerkSecretKey", {
      secretName: `goosetown/${env}/clerk_secret_key`,
      description: "Clerk secret key for GooseTown",
    });

    const townTokenSecret = new secretsmanager.Secret(
      this,
      "TownTokenSecret",
      {
        secretName: `goosetown/${env}/town_token_secret`,
        description: "Town token signing secret",
        generateSecretString: {
          excludePunctuation: true,
          passwordLength: 64,
        },
      }
    );

    const pixelLabApiKey = new secretsmanager.Secret(this, "PixelLabApiKey", {
      secretName: `goosetown/${env}/pixellab_api_key`,
      description: "PixelLab API key for sprite generation",
    });

    const resendApiKey = new secretsmanager.Secret(this, "ResendApiKey", {
      secretName: `goosetown/${env}/resend_api_key`,
      description: "Resend API key for landing page email signups",
    });

    // Shared secret authenticating the API Gateway WebSocket Lambdas' POSTs to
    // the backend's /town/ws-message and /town/ws-disconnect endpoints (#427).
    // Both sides are wired in CDK: api-stack passes the secret NAME to the
    // ws-default / ws-disconnect Lambdas (they fetch it at cold start), and
    // compute-stack injects the VALUE into the backend task.
    const wsLambdaSecret = new secretsmanager.Secret(this, "WsLambdaSecret", {
      secretName: `goosetown/${env}/ws_lambda_secret`,
      description:
        "Shared secret for API Gateway WS Lambdas -> backend /town/ws-message + /town/ws-disconnect",
      generateSecretString: {
        excludePunctuation: true,
        passwordLength: 48,
      },
    });

    // Shared secret authenticating the sprite worker's POSTs to the backend's
    // /town/internal/sprite-event endpoint. BOTH sides of this seam are wired
    // here in CDK.
    const spriteEventSecret = new secretsmanager.Secret(
      this,
      "SpriteEventSecret",
      {
        secretName: `goosetown/${env}/sprite_event_secret`,
        description:
          "Shared secret for sprite worker -> backend /town/internal/sprite-event",
        generateSecretString: {
          excludePunctuation: true,
          passwordLength: 48,
        },
      }
    );

    this.secrets = {
      clerkIssuer,
      clerkSecretKey,
      townTokenSecret,
      pixelLabApiKey,
      resendApiKey,
      spriteEventSecret,
      wsLambdaSecret,
    };

    // Outputs
    new cdk.CfnOutput(this, "ClerkIssuerArn", {
      value: clerkIssuer.secretArn,
    });
    new cdk.CfnOutput(this, "ClerkSecretKeyArn", {
      value: clerkSecretKey.secretArn,
    });
  }
}
