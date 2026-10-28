module counter (
    input  logic       clk,
    input  logic       rst,
    input  logic       en,
    output logic [3:0] count
);
    always_ff @(posedge clk) begin
        if (rst)     count <= 4'd0;
        else if (en) count <= count + 4'd1;
    end
endmodule
