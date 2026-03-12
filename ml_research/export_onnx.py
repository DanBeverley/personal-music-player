import torch
from train_ssm import ChorusDetectorCNN

def export_model():
    model = ChorusDetectorCNN()
    model.eval()
    
    # Dummy input representing an SSM image (1 channel, 128x128)
    dummy_input = torch.randn(1, 1, 128, 128, requires_grad=True)
    
    # Export the model
    torch.onnx.export(model,
                      dummy_input,
                      "chorus_detector.onnx",
                      export_params=True,
                      opset_version=10,
                      do_constant_folding=True,
                      input_names=['input'],
                      output_names=['output'],
                      dynamic_axes={'input': {0: 'batch_size'},
                                    'output': {0: 'batch_size'}})
    print("Exported chorus_detector.onnx successfully.")

if __name__ == "__main__":
    export_model()
