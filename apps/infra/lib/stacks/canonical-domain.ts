/** Existing live canonical resources require CloudFormation adoption before deploy. */
export const canonicalZoneId = "Z09913772LJJ6H4X9JJX0";
export const canonicalZoneName = "thelightbulb.company";
export const canonicalCertificateArn = "arn:aws:acm:us-east-1:877352799272:certificate/817898d5-f4b1-4385-8343-2b317ba439a8";
export function canonicalHost(kind: "api" | "ws" | "assets", env: string): string {
  return `${kind}${env === "prod" ? "" : "-dev"}.goosetown.thelightbulb.company`;
}
export function frontendOrigin(env: string): string {
  return `https://${env === "prod" ? "" : "dev."}goosetown.thelightbulb.company`;
}
