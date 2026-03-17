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

    this.secrets = {
      clerkIssuer,
      clerkSecretKey,
      townTokenSecret,
      pixelLabApiKey,
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
