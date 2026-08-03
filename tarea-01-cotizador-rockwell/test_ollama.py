from langchain_ollama import ChatOllama

llm = ChatOllama(model="llama3.2", temperature=0)
print(llm.invoke("Responde solo con la palabra: OK").content)