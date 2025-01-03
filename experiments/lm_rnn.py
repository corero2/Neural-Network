from pathlib import Path
import json

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

from tqdm import tqdm
import pandas as pd

with open("datasets/shakespeare.txt", encoding="utf8") as f:
    text = f.read()

train_size = len(text) * 7 // 10
train_set = text[:train_size]
val_set = text[train_size:]

# vocab
vocab = sorted(set(train_set))
vocab_size = len(vocab)
print("vocab size:", vocab_size)

char2idx = {char: idx for idx, char in enumerate(vocab)}


class LMDataset(Dataset):
    def __init__(self, dataset, max_length):
        xs = []
        ys = []
        for i in range(0, len(dataset) - max_length - 2, max_length):
            txt = dataset[i : i + max_length + 1]
            indices = [char2idx[char] for char in txt]
            xs.append(indices[:-1])
            ys.append(indices[1:])

        self.xs = torch.LongTensor(xs)
        self.ys = torch.LongTensor(ys)

    def __len__(self):
        return len(self.xs)

    def __getitem__(self, idx):
        return self.xs[idx], self.ys[idx]


class Datasets:
    def __init__(self, batch_size, max_length=100):
        self.train_loader = DataLoader(
            LMDataset(train_set, max_length), batch_size=batch_size, shuffle=True
        )
        self.val_loader = DataLoader(
            LMDataset(val_set, max_length), batch_size=batch_size * 2, shuffle=True
        )


class LstmLM(nn.Module):
    def __init__(self, num_classes, num_embeddings, embedding_dim, hidden_size):
        super(LstmLM, self).__init__()

        self.num_classes = num_classes
        self.num_embeddings = num_embeddings
        self.embedding_dim = embedding_dim
        self.hidden_size = hidden_size

        self.embedding = nn.Embedding(
            num_embeddings=num_embeddings, embedding_dim=embedding_dim
        )
        self.lstm = nn.LSTM(
            input_size=embedding_dim, hidden_size=hidden_size, batch_first=True
        )
        self.fc = nn.Linear(in_features=hidden_size, out_features=num_classes)

    def forward(self, x):
        """
        Args:
            x: [N,L]
        """
        o1 = self.embedding(x)
        o2 = self.lstm(o1)
        o3 = self.fc(o2[0])
        o4 = torch.log_softmax(o3, dim=-1)
        return o4

    def predict(self, x, states=None, temperature=1.0):
        """
        Args:
            x: [N,L]
        """
        o1 = self.embedding(x)
        o2 = self.lstm(o1, states)
        o3 = self.fc(o2[0])
        if temperature != 1.0:
            o3 /= temperature
        o4 = torch.softmax(o3, dim=-1)
        return o4, o2[1]


class Trainer:
    def __init__(self, datasets, model, optimizer, loss_fn, results_path="results"):
        self.datasets = datasets
        self.model = model
        self.optimizer = optimizer
        self.loss_fn = loss_fn
        self.results_path = Path(results_path)

        self.train_df = None

        # device
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        print("using device: ", self.device)
        model.to(self.device)

    def train_epoch(self, msg_format):
        self.model.train()

        losses = []
        progress_bar = tqdm(self.datasets.train_loader)
        for tokens, target in progress_bar:
            self.optimizer.zero_grad()

            _tokens = tokens.to(self.device)
            masks = _tokens.gt(0.0)
            output = self.model(_tokens)
            batch_losses = self.loss(output, target.to(self.device), masks)
            loss = batch_losses / masks.sum()

            loss.backward()
            self.optimizer.step()

            progress_bar.set_description(msg_format.format(loss.item()))

            losses.append(loss.item())
        return losses

    def validate(self):
        self.model.eval()

        tokens_count = 0
        val_loss = 0
        correct = 0
        with torch.no_grad():
            for tokens, target in self.datasets.val_loader:
                _tokens = tokens.to(self.device)
                _target = target.to(self.device)

                masks = _tokens.gt(0.0)
                tokens_count += masks.sum()

                output = self.model(_tokens)
                val_loss += self.loss(output, _target, masks)

                correct += (output.argmax(dim=-1).eq(_target) * masks).sum()

        return (val_loss / tokens_count).item(), (correct / tokens_count).item()

    def loss(self, output, target, masks):
        masked_losses = self.loss_fn(output.permute(dims=(0, 2, 1)), target) * masks
        return masked_losses.sum()

    def train(self, num_epoch):
        val_loss, accuracy = self.validate()
        all_losses = [[None, val_loss, accuracy]]

        for epoch in range(num_epoch):
            # train
            train_losses = self.train_epoch(
                f"train {epoch}/{num_epoch} -- loss: {{:3.2f}}, val_loss: {val_loss:3.2f}, accuracy: {accuracy:.1%}"
            )

            # validate
            val_loss, accuracy = self.validate()
            all_losses.extend([[train_loss, None, None] for train_loss in train_losses])
            all_losses.append([None, val_loss, accuracy])
        print(f"final accuracy: {accuracy}")

        self.save_model()
        self.train_df = pd.DataFrame(
            data=all_losses, columns=["train_loss", "val_loss", "accuracy"]
        )
        self.train_df.to_csv(self.results_path / "train.csv", index=False)

    def save_model(self):
        self.results_path.mkdir(exist_ok=True)
        torch.save(self.model.state_dict(), self.results_path / "model.pth")

        with open(self.results_path / "vocab.json", "w", encoding="utf8") as f:
            f.write(json.dumps(vocab))

    def plot(self):
        import matplotlib.pyplot as plt

        self.train_df[["train_loss", "val_loss"]].ffill().plot(
            title="loss", grid=True, logy=False
        )
        self.train_df[["accuracy"]].dropna().plot(title="accuracy", grid=True)
        plt.show()


def train():
    torch.manual_seed(0)
    datasets = Datasets(300, max_length=60)

    model = LstmLM(
        len(vocab), num_embeddings=len(vocab), embedding_dim=50, hidden_size=50
    )

    loss_fn = torch.nn.NLLLoss(reduction="none")
    optimizer = torch.optim.Adam(model.parameters(), lr=0.003, weight_decay=0.0006)
    trainer = Trainer(
        datasets,
        model=model,
        optimizer=optimizer,
        loss_fn=loss_fn,
        results_path="results",
    )

    trainer.train(num_epoch=50)
    trainer.plot()


class Predictor:
    def __init__(self, model_dir):
        with open(model_dir / "vocab.json", "r", encoding="utf8") as f:
            vocab = json.load(f)

        model = LstmLM(
            len(vocab), num_embeddings=len(vocab), embedding_dim=50, hidden_size=50
        )

        # load model
        model.load_state_dict(torch.load(model_dir / "model.pth"))
        self.model = model.to(torch.device("cpu"))

    def predict(self, text, max_length=100, temperature=1.0):
        tokens = torch.LongTensor([char2idx[char] for char in text])

        generated = ""

        self.model.eval()
        with torch.no_grad():
            states = None

            for _ in range(max_length):
                output, states = self.model.predict(
                    tokens, states, temperature=temperature
                )
                # predicted_idx = output[-1].argmax(dim=-1).item()
                predicted_idx = output[-1].multinomial(1).item()

                predicted_char = vocab[predicted_idx]
                generated += predicted_char

                tokens = torch.LongTensor([predicted_idx])

        return generated


def predict():
    predictor = Predictor(Path("results"))
    text = predictor.predict("\n", max_length=100, temperature=0.5)
    print(text)


if __name__ == "__main__":
    train()
    # predict()