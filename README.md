Part of [GPT‑RAG](https://aka.ms/gpt-rag)

<!-- 
page_type: sample
languages:
- azdeveloper
- powershell
- bicep
products:
- azure
- azure-ai-foundry
- azure-openai
- azure-ai-search
urlFragment: GPT-RAG
name: Multi-repo ChatGPT and Enterprise data with Azure OpenAI and AI Search
description: GPT-RAG core is a Retrieval-Augmented Generation pattern running in Azure, using Azure AI Search for retrieval and Azure OpenAI large language models to power ChatGPT-style and Q&A experiences.
-->
# GPT-RAG Web UI

Part of the [GPT-RAG](https://github.com/Azure/gpt-rag) solution.

This repo provides a user interface built with [Chainlit](https://www.chainlit.io/) to interact with GPT-powered retrieval-augmented generation systems. It is designed to work seamlessly with the Orchestrator backend and supports customization and theming.

## Prerequisites

## Prerequisites

Before deploying the web application, you must provision the infrastructure as described in the [GPT-RAG](https://github.com/azure/gpt-rag/tree/feature/vnext-architecture) repo. This includes creating all necessary Azure resources required to support the application runtime.


## How to deploy the web app

```shell
azd env refresh
azd deploy 
````
> [!IMPORTANT]
> When running `azd env refresh`, make sure to use the **same subscription**, **resource group**, and **environment name** that you used during the infrastructure deployment. This ensures consistency across components.

## 🎨 Customization

- Modify theme in `public/theme.json`
- Customize layout with `public/custom.css`
- Adjust app behavior in `.chainlit/config.toml`

## 🤝 Contributing

We appreciate contributions! See [CONTRIBUTING.md](./CONTRIBUTING.md) for guidelines on the Contributor License Agreement (CLA), code of conduct, and submitting pull requests.

## Trademarks

This project may contain trademarks or logos. Authorized use of Microsoft trademarks or logos must follow [Microsoft’s Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general). Modified versions must not imply sponsorship or cause confusion. Third-party trademarks are subject to their own policies.