import boto3, json

client = boto3.client("bedrock-runtime", region_name="us-west-2")
resp = client.invoke_model(
    modelId="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    body=json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Say hello and confirm you are working."}],
    }),
)
print(json.loads(resp["body"].read())["content"][0]["text"])