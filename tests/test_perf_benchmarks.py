import ast
import os
import subprocess

import pytest

from src.enums import LangChainAction
from tests.test_inference_servers import run_h2ogpt_docker
from tests.utils import wrap_test_forked, get_inf_server, get_inf_port, get_sha
from src.utils import download_simple


@pytest.mark.parametrize("backend", [
    'transformers',
    # 'tgi',
    # 'mixed',
])
@pytest.mark.parametrize("base_model", [
    'h2oai/h2ogpt-4096-llama2-7b-chat',
    # 'h2oai/h2ogpt-4096-llama2-13b-chat',
])
@pytest.mark.parametrize("task", [
    # 'summary',
    # 'generate',
    'summary_and_generate'
])
@pytest.mark.parametrize("bits", [
    16,
    8,
    4,
], ids=["16-bit", "8-bit", "4-bit"])
@pytest.mark.parametrize("ngpus", [
    1, 2, 4, 8
], ids=["1 GPU", "2 GPUs", "4 GPUs", "8 GPUs"])
@pytest.mark.need_tokens
@wrap_test_forked
def test_perf_benchmarks(backend, base_model, task, bits, ngpus):
    bench_dict = locals()
    from datetime import datetime
    import json
    os.environ['CUDA_VISIBLE_DEVICES'] = "0" if ngpus == 1 else ",".join([str(x) for x in range(ngpus)])
    import torch
    n_gpus = torch.cuda.device_count()
    if n_gpus != ngpus:
        return
    git_sha = (
        subprocess.check_output("git rev-parse HEAD", shell=True)
        .decode("utf-8")
        .strip()
    )
    bench_dict["date"] = datetime.now().strftime("%m/%d/%Y %H:%M:%S")
    bench_dict["git_sha"] = git_sha[:8]
    bench_dict["n_gpus"] = n_gpus
    bench_dict["gpus"] = [torch.cuda.get_device_name(i) for i in range(n_gpus)]

    # launch server(s)
    docker_hash1 = None
    docker_hash2 = None
    max_new_tokens = 4096
    results_file = "./perf.json"
    try:
        h2ogpt_args = dict(base_model=base_model,
             chat=True, gradio=True, num_beams=1, block_gradio_exit=False, verbose=True,
             load_half=bits == 16,
             load_8bit=bits == 8,
             load_4bit=bits == 4,
             langchain_mode='MyData',
             use_auth_token=True,
             max_new_tokens=max_new_tokens,
             use_gpu_id=ngpus == 1,
             )
        if backend == 'transformers':
            from src.gen import main
            main(**h2ogpt_args)
        elif backend == 'tgi':
            if bits != 16:
                pytest.xfail("Quantization not yet supported in TGI")
            from tests.test_inference_servers import run_docker
            # HF inference server
            gradio_port = get_inf_port()
            inf_port = gradio_port + 1
            inference_server = 'http://127.0.0.1:%s' % inf_port
            docker_hash1 = run_docker(inf_port, base_model, low_mem_mode=False)  # don't do low-mem, since need tokens for summary
            import time
            time.sleep(30)
            os.system('docker logs %s | tail -10' % docker_hash1)

            # h2oGPT server
            docker_hash2 = run_h2ogpt_docker(gradio_port, base_model, inference_server=inference_server, max_new_tokens=max_new_tokens)
            time.sleep(30)  # assumes image already downloaded, else need more time
            os.system('docker logs %s | tail -10' % docker_hash2)
        elif backend == 'mixed':
            if bits != 16:
                pytest.xfail("Quantization not yet supported in TGI")
            from tests.test_inference_servers import run_docker
            # HF inference server
            gradio_port = get_inf_port()
            inf_port = gradio_port + 1
            inference_server = 'http://127.0.0.1:%s' % inf_port
            docker_hash1 = run_docker(inf_port, base_model, low_mem_mode=False)  # don't do low-mem, since need tokens for summary
            import time
            time.sleep(30)
            os.system('docker logs %s | tail -10' % docker_hash1)

            from src.gen import main
            main(**h2ogpt_args)
        else:
            raise NotImplementedError("backend %s not implemented" % backend)

        # get file for client to upload
        url = 'https://cdn.openai.com/papers/whisper.pdf'
        test_file1 = os.path.join('/tmp/', 'my_test_pdf.pdf')
        download_simple(url, dest=test_file1)

        # PURE client code
        from gradio_client import Client
        client = Client(get_inf_server())

        if "summary" in task:
            # upload file(s).  Can be list or single file
            test_file_local, test_file_server = client.predict(test_file1, api_name='/upload_api')
            assert os.path.normpath(test_file_local) != os.path.normpath(test_file_server)

            chunk = True
            chunk_size = 512
            langchain_mode = 'MyData'
            res = client.predict(test_file_server, chunk, chunk_size, langchain_mode, api_name='/add_file_api')
            assert res[0] is None
            assert res[1] == langchain_mode
            # assert os.path.basename(test_file_server) in res[2]
            assert res[3] == ''

            # ask for summary, need to use same client if using MyData
            api_name = '/submit_nochat_api'  # NOTE: like submit_nochat but stable API for string dict passing
            kwargs = dict(langchain_mode=langchain_mode,
                          langchain_action="Summarize",  # uses full document, not vectorDB chunks
                          top_k_docs=4,  # -1 == entire pdf
                          document_subset='Relevant',
                          document_choice='All',
                          max_new_tokens=max_new_tokens,
                          max_time=300,
                          do_sample=False,
                          prompt_summary='Summarize into single paragraph',
                          )

            import time
            t0 = time.time()
            res = client.predict(
                str(dict(kwargs)),
                api_name=api_name,
            )
            t1 = time.time()
            res = ast.literal_eval(res)
            response = res['response']
            sources = res['sources']
            size_summary = os.path.getsize(test_file1)
            # print(response)
            print("Time to summarize %s bytes into %s bytes: %.4f" % (size_summary, len(response), t1-t0))
            bench_dict["summarize_input_len_bytes"] = size_summary
            bench_dict["summarize_output_len_bytes"] = len(response)
            bench_dict["summarize_time"] = t1 - t0
            # bench_dict["summarize_tokens_per_sec"] = res['tokens/s']
            assert 'my_test_pdf.pdf' in sources

        if "generate" in task:
            api_name = '/submit_nochat_api'  # NOTE: like submit_nochat but stable API for string dict passing
            kwargs = dict(prompt_summary="Write a poem about water.")
            import time
            t0 = time.time()
            res = client.predict(
                str(dict(kwargs)),
                api_name=api_name,
            )
            t1 = time.time()
            res = ast.literal_eval(res)
            response = res['response']
            # print(response)
            print("Time to generate %s bytes: %.4f" % (len(response), t1-t0))
            bench_dict["generate_output_len_bytes"] = len(response)
            bench_dict["generate_time"] = t1 - t0
            # bench_dict["generate_tokens_per_sec"] = res['tokens/s']
    except BaseException as e:
        if 'CUDA out of memory' in str(e):
            e = "OOM"
        bench_dict["exception"] = str(e)
        raise
    finally:
        with open(results_file, mode="a") as f:
            f.write(json.dumps(bench_dict) + "\n")
        if backend == "tgi":
            if docker_hash1:
                os.system("docker stop %s" % docker_hash1)
            if docker_hash2:
                os.system("docker stop %s" % docker_hash2)