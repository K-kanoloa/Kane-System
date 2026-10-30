import manifest from "../manifest";
export function GET() {
  return Response.json(manifest(), { headers: { "Content-Type": "application/manifest+json" } });
}
