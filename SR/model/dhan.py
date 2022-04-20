from model import common

import torch.nn as nn
import torch


def make_model(args, parent=False):
    return RCAN_HAN(args)


class LAM_Module(nn.Module):
    """ Layer attention module"""

    def __init__(self, in_dim, n_resgroups):
        super(LAM_Module, self).__init__()
        self.chanel_in = in_dim
        self.n_resgroups = n_resgroups

        self.pos_dec = nn.Parameter(torch.Tensor([0.5 for i in range(n_resgroups)]), requires_grad=True)
        self.pos_int = nn.Parameter(torch.Tensor([i for i in range(n_resgroups)]), requires_grad=False)

        self.length_dec = nn.Parameter(torch.Tensor([0.5 for i in range(n_resgroups)]), requires_grad=True)
        self.length_int = nn.Parameter(torch.Tensor([1 for i in range(n_resgroups)]), requires_grad=False)

        self.nl_conv = nn.Conv2d(in_dim * n_resgroups, in_dim * n_resgroups, 1, 1, 0)
        self.softmax = nn.Softmax(dim=2)

    def bi_interpolate(self, key, pos_dec, pos_int, length_dec, length_int):
        m, n, chw = key.size()
        key_padded = torch.cat((key, key), dim=1)
        key_short = key_padded[:, pos_int:pos_int + length_int, :] * (1 - pos_dec) + key_padded[:, pos_int + 1:pos_int + 1 + length_int, :] * pos_dec
        key_long = key_padded[:, pos_int:pos_int + length_int + 1, :] * (1 - pos_dec) + key_padded[:, pos_int + 1:pos_int + 1 + length_int + 1, :] * pos_dec
        short_pad_zero = torch.nn.Parameter(torch.zeros(m, 1, chw), requires_grad=False)
        key_short_padded = torch.cat((key_short, short_pad_zero), dim=1)
        searched_key = key_short_padded * (1 - length_dec) + key_long * length_dec

        return searched_key

    def forward(self, x):
        """
            inputs :
                x : input feature maps( B X N X C X H X W)
            returns :
                out : attention value + input feature
                attention: B X N X N
        """
        m_batchsize, N, C, height, width = x.size()

        proj_query = x.view(m_batchsize, N, -1)
        proj_key = x.view(m_batchsize, N, -1).permute(0, 2, 1)

        for i in range(self.n_resgroups):
            query = proj_query[:, i, :]
            key = self.bi_interpolate(proj_key, self.pos_dec[i], self.pos_int[i], self.length_dec[i], self.length_int[i])
            attention_map = self.softmax(torch.bmm(query, key.permute(0, 2, 1)))
            attention_out = torch.bmm(attention_map, key)
            if i == 0:
                final_attention_out = attention_out
            else:
                final_attention_out = torch.cat((final_attention_out, attention_out), dim=1)

        final_attention_out = self.nl_conv(final_attention_out.view(m_batchsize, -1, height, width))

        out = final_attention_out.view(m_batchsize, N, C, height, width) + x

        out = out.view(m_batchsize, -1, height, width)

        return out


class Channel_Spatial_Attention_Module(nn.Module):
    def __init__(self, initial_gamma, fix_gamma=False):
        super(Channel_Spatial_Attention_Module, self).__init__()
        self.conv = nn.Conv3d(1, 1, 3, 1, 1)
        self.sigmoid = nn.Sigmoid()
        self.gamma = nn.Parameter(torch.tensor([initial_gamma]).float(), requires_grad=not fix_gamma)

    def forward(self, x):
        m_batchsize, C, height, width = x.size()
        out = x.unsqueeze(1)
        out = self.sigmoid(self.conv(out))
        out = self.gamma * out
        out = out.view(m_batchsize, -1, height, width)
        x = x * out + x
        return x


## Channel Attention (CA) Layer
class CALayer(nn.Module):
    def __init__(self, channel, reduction=16):
        super(CALayer, self).__init__()
        # global average pooling: feature --> point
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        # feature channel downscale and upscale --> channel weight
        self.conv_du = nn.Sequential(
            nn.Conv2d(channel, channel // reduction, 1, padding=0, bias=True),
            nn.ReLU(inplace=True),
            nn.Conv2d(channel // reduction, channel, 1, padding=0, bias=True),
            nn.Sigmoid()
        )

    def forward(self, x):
        y = self.avg_pool(x)
        y = self.conv_du(y)
        return x * y


## Residual Channel Attention Block (RCAB)
class RCAB(nn.Module):
    def __init__(
            self, conv, n_feat, kernel_size, reduction,
            bias=True, bn=False, act=nn.ReLU(True), res_scale=1):

        super(RCAB, self).__init__()
        modules_body = []
        for i in range(2):
            modules_body.append(conv(n_feat, n_feat, kernel_size, bias=bias))
            if bn: modules_body.append(nn.BatchNorm2d(n_feat))
            if i == 0: modules_body.append(act)
        modules_body.append(CALayer(n_feat, reduction))
        self.body = nn.Sequential(*modules_body)
        self.res_scale = res_scale

    def forward(self, x):
        res = self.body(x)
        # res = self.body(x).mul(self.res_scale)
        res += x
        return res


## Residual Group (RG)
class ResidualGroup(nn.Module):
    def __init__(self, conv, n_feat, kernel_size, reduction, act, res_scale, n_resblocks):
        super(ResidualGroup, self).__init__()
        modules_body = []
        modules_body = [
            RCAB(
                conv, n_feat, kernel_size, reduction, bias=True, bn=False, act=nn.ReLU(True), res_scale=1) \
            for _ in range(n_resblocks)]
        modules_body.append(conv(n_feat, n_feat, kernel_size))
        self.body = nn.Sequential(*modules_body)

    def forward(self, x):
        res = self.body(x)
        res += x
        return res


## Residual Channel Attention Network (RCAN)
class RCAN_HAN(nn.Module):
    def __init__(self, args, conv=common.default_conv):
        super(RCAN_HAN, self).__init__()

        n_resgroups = args.n_resgroups
        n_resblocks = args.n_resblocks
        n_feats = args.n_feats
        kernel_size = 3
        reduction = args.reduction
        scale = args.scale[0]
        act = nn.ReLU(True)

        # RGB mean for DIV2K
        rgb_mean = (0.4488, 0.4371, 0.4040)
        rgb_std = (1.0, 1.0, 1.0)
        self.sub_mean = common.MeanShift(args.rgb_range, rgb_mean, rgb_std)

        # define head module
        modules_head = [conv(args.n_colors, n_feats, kernel_size)]

        # define body module
        modules_body = [
            ResidualGroup(
                conv, n_feats, kernel_size, reduction, act=act, res_scale=args.res_scale, n_resblocks=n_resblocks) \
            for _ in range(n_resgroups)]

        modules_body.append(conv(n_feats, n_feats, kernel_size))

        # define tail module
        modules_tail = [
            common.Upsampler(conv, scale, n_feats, act=False),
            conv(n_feats, args.n_colors, kernel_size)]

        self.add_mean = common.MeanShift(args.rgb_range, rgb_mean, rgb_std, 1)

        self.head = nn.Sequential(*modules_head)
        self.body = nn.Sequential(*modules_body)
        self.tail = nn.Sequential(*modules_tail)
        self.la = LAM_Module(n_feats, n_resgroups)
        self.fusion_conv = nn.Conv2d(n_feats * 10, n_feats, 1, 1, 0)


    def forward(self, x):
        x = self.sub_mean(x)
        x = self.head(x)
        res = x
        # pdb.set_trace()
        for name, midlayer in self.body._modules.items():
            # print(name)
            if name == '0':
                res = midlayer(res)
                midlayer_out = res.unsqueeze(1)
####### edit here
            else:
                if name != '10':
                    res = midlayer(res)
                    midlayer_out = torch.cat([res.unsqueeze(1), midlayer_out], 1)
                else:
                    res = self.la(midlayer_out)
                    res = self.fusion_conv(res)
                    res = midlayer(res)

        res += x

        x = self.tail(res)
        x = self.add_mean(x)

        return x

    def load_state_dict(self, state_dict, strict=False):
        own_state = self.state_dict()
        for name, param in state_dict.items():
            if name in own_state:
                if isinstance(param, nn.Parameter):
                    param = param.data
                try:
                    own_state[name].copy_(param)
                except Exception:
                    if name.find('tail') >= 0:
                        print('Replace pre-trained upsampler to new one...')
                    else:
                        raise RuntimeError('While copying the parameter named {}, '
                                           'whose dimensions in the model are {} and '
                                           'whose dimensions in the checkpoint are {}.'
                                           .format(name, own_state[name].size(), param.size()))
            elif strict:
                if name.find('tail') == -1:
                    raise KeyError('unexpected key "{}" in state_dict'
                                   .format(name))

        if strict:
            missing = set(own_state.keys()) - set(state_dict.keys())
            if len(missing) > 0:
                raise KeyError('missing keys in state_dict: "{}"'.format(missing))