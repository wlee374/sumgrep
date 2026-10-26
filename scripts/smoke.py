import ollama
from qdrant_client import QdrantClient, models

texts = ["retries a request with exponential backoff",
         "parses a CSV file into rows"]
vecs = ollama.embed(model="bge-m3", input=texts)["embeddings"]
q = ollama.embed(model="bge-m3",
                 input=["a function that retries failed calls"])["embeddings"][0]

c = QdrantClient(url="http://localhost:6333")
if c.collection_exists("smoke"):
    c.delete_collection("smoke")
c.create_collection("smoke", vectors_config=models.VectorParams(
    size=len(vecs[0]), distance=models.Distance.COSINE))
c.upsert("smoke", points=[
    models.PointStruct(id=i, vector=v, payload={"text": t})
    for i, (v, t) in enumerate(zip(vecs, texts))])
hit = c.query_points("smoke", query=q, limit=1).points[0]
print(f"nearest: {hit.payload['text']!r}  (score {hit.score:.3f})")
c.delete_collection("smoke")